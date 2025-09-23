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

# --- Use the definitive path from the working build script ---
CHROME_EXECUTABLE_PATH = "/opt/render/project/.render/chrome/opt/google/chrome/google-chrome"
REPO_URL = "https://github.com/dteviot/WebToEpub.git"
REPO_DIR = "webtoepub_repo"

# This JS code is a helper to load and execute the external parser scripts
# inside the browser's context.
PARSER_RUNNER_JS = """
async (parserScript, url) => {
    try {
        let activeParserInstance = null;
        const parserFactory = {
            register: (domains, parser) => {
                // We create an instance immediately to use it
                activeParserInstance = new parser(url, document);
            }
        };

        // Execute the parser script from the database to trigger the register function
        eval(parserScript);

        if (activeParserInstance) {
            const parser = activeParserInstance;
            
            // The extension's parsers can determine if a page is a content page or chapter list
            if (parser.isChapterUrl && parser.isChapterUrl(url)) {
                 const contentElement = await parser.getContent();
                 return {
                    type: 'content',
                    html: contentElement.innerHTML
                 };
            } else {
                const chapters = await parser.getChapters();
                const novelTitle = parser.getNovelTitle ? parser.getNovelTitle() : document.title;
                return {
                    type: 'chapters',
                    title: novelTitle,
                    chapters: chapters.map(ch => ({ title: ch.title, url: ch.url }))
                };
            }
        }
        return { error: 'No parser was registered or activated by the script.' };
    } catch (error) {
        return { error: `JavaScript execution failed: ${error.toString()}` };
    }
}
"""

async def update_parsers_from_github():
    """
    Clones/pulls the repo and scans every parser file to extract the domains
    it registers, storing this mapping in the database.
    """
    if os.path.exists(REPO_DIR):
        repo = git.Repo(REPO_DIR)
        origin = repo.remotes.origin
        origin.pull()
        logger.info("Pulled latest changes from WebToEpub repository.")
    else:
        git.Repo.clone_from(REPO_URL, REPO_DIR)
        logger.info("Cloned WebToEpub repository.")

    parsers_dir = os.path.join(REPO_DIR, "plugin", "js", "parsers")
    if not os.path.isdir(parsers_dir):
        return 0
        
    parser_files = [f for f in os.listdir(parsers_dir) if f.endswith('.js')]
    
    parsers_to_save = []
    # Regex to find all domains in parserFactory.register(["domain1", "domain2"], Parser)
    domain_regex = re.compile(r'parserFactory\.register\(\s*(\[.*?\]|\".*?\")\s*,', re.DOTALL)
    
    for filename in parser_files:
        filepath = os.path.join(parsers_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            match = domain_regex.search(content)
            if match:
                # The matched group is either a JS array '["a", "b"]' or a single string '"a"'
                domains_str = match.group(1).replace("'", '"')
                # A safe way to parse the string into a Python list
                try:
                    domains = [d.strip() for d in domains_str.strip('[]').replace('"', '').split(',') if d.strip()]
                    if domains:
                        parsers_to_save.append({
                            "filename": filename,
                            "domains": domains,
                            "script": content
                        })
                except Exception as e:
                    logger.warning(f"Could not parse domains from {filename}: {e}")

    if parsers_to_save:
        save_parsers_from_repo(parsers_to_save)
        logger.info(f"Successfully saved {len(parsers_to_save)} parsers to the database.")
    return len(parsers_to_save)


async def get_chapter_list(url: str, user_id: int) -> (str, list, bool):
    """
    Fetches chapter list by finding and EXECUTING the correct site-specific JavaScript parser.
    """
    if not os.path.exists(CHROME_EXECUTABLE_PATH):
        raise FileNotFoundError(f"FATAL: Chrome executable not found: {CHROME_EXECUTABLE_PATH}")

    repo_parser = get_repo_parser(url)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=CHROME_EXECUTABLE_PATH,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
        )
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until='networkidle', timeout=60000)
        except Exception as e:
            await browser.close()
            raise IOError(f"Failed to navigate to URL: {e}")

        if repo_parser:
            logger.info(f"Executing parser '{repo_parser['filename']}' for {url}")
            result = await page.evaluate(PARSER_RUNNER_JS, repo_parser['script'], url)
            
            if result and 'error' not in result and result.get('type') == 'chapters':
                await browser.close()
                logger.info(f"Parser successfully extracted {len(result['chapters'])} chapters.")
                chapters = result['chapters']
                for chapter in chapters:
                    chapter['url'] = urljoin(url, chapter['url']) # Ensure URLs are absolute
                    chapter['selected'] = True
                return result['title'], chapters, True
            else:
                logger.error(f"Parser '{repo_parser['filename']}' failed: {result.get('error', 'Unknown error')}")

        # --- GENERIC FALLBACK (if no parser or if parser fails) ---
        logger.warning("No parser found or parser failed. Falling back to generic scraping.")
        html_content = await page.content()
        await browser.close()
        soup = BeautifulSoup(html_content, 'html.parser')
        title = (soup.find('title').string or 'Untitled').strip()
        links = soup.find_all('a', href=True)
        chapters = [{'title': link.text.strip(), 'url': urljoin(url, link['href']), 'selected': True} 
                    for link in links if link.text.strip() and re.search(r'chapter|ep\d+|ch\.\d+', link.text.lower(), re.I)]
        
        if not chapters:
            chapters = [{'title': "Full Page Content", 'url': url, 'selected': True}]
            
        return title, chapters, False


async def create_epub_from_chapters(chapters: list, title: str, settings: dict) -> (str, str):
    """Creates an EPUB, using the correct JS parser to get clean chapter content."""
    
    final_filename = re.sub(r'[\\/*?:"<>|]', "", title)
    book = epub.EpubBook()
    book.set_identifier('id' + title)
    book.set_title(title)
    book.set_language('en')
    book.add_author('WebToEpub Bot')

    book_spine = ['nav']
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=CHROME_EXECUTABLE_PATH,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
        )
        page = await browser.new_page()

        for i, chapter_data in enumerate(chapters):
            if not chapter_data.get('selected', False):
                continue
            try:
                logger.info(f"Fetching chapter: {chapter_data['title']} ({chapter_data['url']})")
                await page.goto(chapter_data['url'], wait_until='networkidle', timeout=60000)
                
                repo_parser = get_repo_parser(chapter_data['url'])
                chapter_html_content = ''

                if repo_parser:
                    logger.info(f"Executing getContent() from '{repo_parser['filename']}'...")
                    result = await page.evaluate(PARSER_RUNNER_JS, repo_parser['script'], chapter_data['url'])
                    if result and 'error' not in result and result.get('type') == 'content':
                        chapter_html_content = result['html']
                    else:
                        logger.warning(f"Parser content extraction failed: {result.get('error', 'N/A')}. Falling back.")
                        chapter_html_content = await page.content() # Fallback
                else:
                    chapter_html_content = await page.content()

                # The JS parser provides clean HTML, so we just wrap it.
                # A generic fallback is not needed as the parser's output is trusted.
                final_html = f"<h1>{chapter_data['title']}</h1>{chapter_html_content}"
                
                epub_chapter = epub.EpubHtml(title=chapter_data['title'], file_name=f'chap_{i+1}.xhtml', lang='en')
                epub_chapter.content = final_html
                book.add_item(epub_chapter)
                book_spine.append(epub_chapter)

            except Exception as e:
                logger.error(f"FATAL error processing chapter '{chapter_data['title']}': {e}", exc_info=True)
                continue
        
        await browser.close()

    book.spine = book_spine
    book.toc = [(epub.Link(c.file_name, c.title, f"chap_{i+1}")) for i, c in enumerate(book.items) if isinstance(c, epub.EpubHtml)]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub_path = f"{final_filename}.epub"
    epub.write_epub(epub_path, book, {})

    return epub_path, final_filename
