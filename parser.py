import asyncio
import os
import re
import git
from ebooklib import epub
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from database import get_custom_parser, get_repo_parser, save_parsers_from_repo
from urllib.parse import urljoin
import logging

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/dteviot/WebToEpub.git"
REPO_DIR = "webtoepub_repo"
PLAYWRIGHT_BROWSERS_PATH = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/opt/render/project/src/.playwright')

def find_chromium_executable():
    """Dynamically finds the Chromium executable with detailed logging."""
    logger.info(f"Searching for Chromium in PLAYWRIGHT_BROWSERS_PATH: {PLAYWRIGHT_BROWSERS_PATH}")
    if not os.path.exists(PLAYWRIGHT_BROWSERS_PATH):
        logger.error(f"Browser path does not exist: {PLAYWRIGHT_BROWSERS_PATH}")
        return None
    
    try:
        dir_contents = os.listdir(PLAYWRIGHT_BROWSERS_PATH)
        logger.info(f"Contents of {PLAYWRIGHT_BROWSERS_PATH}: {dir_contents}")
    except Exception as e:
        logger.error(f"Could not list directory {PLAYWRIGHT_BROWSERS_PATH}: {e}")
        return None

    for item in dir_contents:
        if 'chromium' in item:
            browser_dir = os.path.join(PLAYWRIGHT_BROWSERS_PATH, item)
            potential_paths = [
                os.path.join(browser_dir, 'chrome-linux', 'chrome'),
                os.path.join(browser_dir, 'chrome-linux', 'headless_shell') # Fallback name
            ]
            for path in potential_paths:
                logger.info(f"Checking for executable at: {path}")
                if os.path.exists(path) and os.access(path, os.X_OK):
                    logger.info(f"Found executable and it is runnable: {path}")
                    return path
                elif os.path.exists(path):
                    logger.warning(f"Found file at {path}, but it is NOT executable.")

    logger.error("Completed search. No suitable chromium executable was found.")
    return None

async def update_parsers_from_github():
    """Clones or pulls the WebToEpub repository and updates parsers in the database."""
    if os.path.exists(REPO_DIR):
        repo = git.Repo(REPO_DIR)
        origin = repo.remotes.origin
        origin.pull()
    else:
        git.Repo.clone_from(REPO_URL, REPO_DIR)

    parsers_dir = os.path.join(REPO_DIR, "plugin", "js", "parsers")
    if not os.path.isdir(parsers_dir):
        return 0
        
    parser_files = [f for f in os.listdir(parsers_dir) if f.endswith('.js')]
    
    parsers_to_save = []
    for filename in parser_files:
        with open(os.path.join(parsers_dir, filename), 'r', encoding='utf-8') as f:
            content = f.read()
            domains = re.findall(r"parserFactory\.register\(\s*\"(.*?)\"", content)
            for domain in domains:
                parsers_to_save.append({"domain": domain, "script": content})

    if parsers_to_save:
        save_parsers_from_repo(parsers_to_save)
    return len(parsers_to_save)

async def fetch_page_content(url: str) -> str:
    """Fetches the full HTML content of a web page using Playwright."""
    executable_path = find_chromium_executable()
    if not executable_path:
        raise RuntimeError("Chromium executable not found. Check PLAYWRIGHT_BROWSERS_PATH and build script.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=executable_path)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until='networkidle', timeout=60000)
            content = await page.content()
        finally:
            await browser.close()
        return content

def sanitize_filename(filename):
    """Removes invalid characters from a filename."""
    return re.sub(r'[\\/*?:"<>|]', "", filename)

async def get_chapter_list(url: str, user_id: int) -> (str, list, bool):
    """
    Fetches the chapter list from a URL.
    Returns the novel title, a list of chapters, and a boolean indicating if a specific parser was found.
    """
    html_content = await fetch_page_content(url)
    soup = BeautifulSoup(html_content, 'html.parser')

    title_tag = soup.find('title')
    title = title_tag.string.strip() if title_tag and title_tag.string else 'Untitled'

    chapters = []
    parser_found = False

    custom_parser = get_custom_parser(user_id, url)
    repo_parser = get_repo_parser(url)

    if custom_parser or repo_parser:
        parser_found = True
        toc_selectors = [
            'ul.chapter-list', 'ul.list-chapter', 'div#chapter-list',
            'div.chapter-list', 'div#list dd', 'div.chapter-list-wrapper',
            '.chapter-list a', '.entry-content a', '.post-content a',
        ]
        for selector in toc_selectors:
            toc_element = soup.select_one(selector)
            if toc_element:
                links = toc_element.find_all('a', href=True)
                for link in links:
                    if link.text.strip():
                        chapters.append({'title': link.text.strip(), 'url': link['href']})
                if chapters:
                    break
    
    if not chapters: 
        links = soup.find_all('a', href=True)
        chapters = [{'title': link.text.strip(), 'url': link['href']} for link in links if re.search(r'chapter|ep\d+|ch\.\d+', link.text.lower()) and link.text.strip()]
        if not chapters: 
            chapters = [{'title': "Full Page Content", 'url': url}]

    for chapter in chapters:
        chapter['selected'] = True
        if not chapter['url'].startswith('http'):
             chapter['url'] = urljoin(url, chapter['url'])

    return title, chapters, parser_found

async def create_epub_from_chapters(chapters: list, title: str, settings: dict) -> (str, str):
    """Creates an EPUB from a list of selected chapter dictionaries."""
    
    author = settings.get('author', 'Unknown')
    final_filename = sanitize_filename(title)

    book = epub.EpubBook()
    book.set_identifier('id' + title)
    book.set_title(title)
    book.set_language('en')
    book.add_author(author)

    book_spine = ['nav']
    
    for i, chapter_data in enumerate(chapters):
        try:
            logger.info(f"Fetching chapter: {chapter_data['title']}")
            html_content = await fetch_page_content(chapter_data['url'])
            soup = BeautifulSoup(html_content, 'html.parser')

            content_selectors = ['div#chapter-content', 'div.entry-content', 'div.reading-content', 'div#content', 'article', 'div.post-content']
            content_body = None
            for selector in content_selectors:
                content_body = soup.select_one(selector)
                if content_body:
                    break
            if not content_body:
                content_body = soup.find('body')

            if settings.get('skipImages'):
                 for img_tag in content_body.find_all('img'):
                    img_tag.decompose()

            chapter_html = f"<h1>{chapter_data['title']}</h1>{str(content_body)}"
            
            epub_chapter = epub.EpubHtml(title=chapter_data['title'], file_name=f'chap_{i+1}.xhtml', lang='en')
            epub_chapter.content = chapter_html
            book.add_item(epub_chapter)
            book_spine.append(epub_chapter)

        except Exception as e:
            logger.error(f"Failed to fetch or process chapter '{chapter_data['title']}': {e}")
            continue

    book.spine = book_spine
    book.toc = [(epub.Link(c.file_name, c.title, f"chap_{i+1}")) for i, c in enumerate(book.items) if isinstance(c, epub.EpubHtml)]

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub_path = f"{final_filename}.epub"
    epub.write_epub(epub_path, book, {})

    return epub_path, final_filename
