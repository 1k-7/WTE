import asyncio
import os
import re
import git
from ebooklib import epub
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from database import get_custom_parser, get_repo_parser, save_parsers_from_repo

REPO_URL = "https://github.com/dteviot/WebToEpub.git"
REPO_DIR = "webtoepub_repo"

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
            # This regex finds the domain registration in the parser files
            domains = re.findall(r"parserFactory\.register\(\s*\"(.*?)\"", content)
            for domain in domains:
                parsers_to_save.append({"domain": domain, "script": content})

    if parsers_to_save:
        save_parsers_from_repo(parsers_to_save)
    return len(parsers_to_save)


async def fetch_page_content(url: str) -> str:
    """Fetches the full HTML content of a web page using Playwright."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
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

async def create_epub_from_url(url: str, settings: dict, user_id: int) -> (str, str):
    """Creates an EPUB file from a given URL, using the best available parser."""
    try:
        # NOTE: This is a placeholder for actual JS parser execution.
        # A full implementation would require a JS runtime environment (like Node.js)
        # to execute the parser scripts against the fetched HTML content.
        custom_parser = get_custom_parser(user_id, url)
        repo_parser = get_repo_parser(url)

        if custom_parser:
            print(f"Found custom parser for {url}")
        elif repo_parser:
            print(f"Found repository parser for {url}")
        
        # Fallback to generic parsing if no specific parser is found/run
        html_content = await fetch_page_content(url)
        soup = BeautifulSoup(html_content, 'html.parser')

        title_tag = soup.find('title')
        title = title_tag.string.strip() if title_tag and title_tag.string else 'Untitled'
        
        author = "Unknown"
        author_tag = soup.find('meta', attrs={'name': 'author'})
        if author_tag:
            author = author_tag.get('content', 'Unknown')

        # --- Filename Generation ---
        filename_template = settings.get('CustomFilename', '%Filename%')
        base_filename = sanitize_filename(title) if not settings.get('useFullTitleAsFileName') else sanitize_filename(title)
        
        final_filename = filename_template.replace('%Title%', sanitize_filename(title))
        final_filename = final_filename.replace('%Author%', sanitize_filename(author))
        final_filename = final_filename.replace('%Filename%', base_filename)
        
        book = epub.EpubBook()
        book.set_identifier('id' + str(user_id) + title)
        book.set_title(title)
        book.set_language('en')
        book.add_author(author)

        body_content = soup.find('body')
        if not body_content:
            return None, None
            
        content_html = "<html><head><title>{}</title></head><body>{}</body></html>".format(
            title, body_content.prettify()
        )
        
        if settings.get('skipImages'):
            content_html = re.sub(r'<img.*?>', '', content_html, flags=re.DOTALL)

        c1 = epub.EpubHtml(title='Content', file_name='chap_1.xhtml', lang='en')
        c1.content = content_html

        book.add_item(c1)
        book.toc = (epub.Link('chap_1.xhtml', 'Content', 'intro'),)
        book.spine = ['nav', c1]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        epub_path = f"{sanitize_filename(final_filename)}.epub"
        epub.write_epub(epub_path, book, {})

        return epub_path, sanitize_filename(final_filename)

    except Exception as e:
        print(f"Failed to create EPUB: {e}")
        return None, None
