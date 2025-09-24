import asyncio
import os
import re
import git
from ebooklib import epub
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from database import get_custom_parser, get_repo_parser, save_parsers_from_repo
from urllib.parse import urljoin, quote
import logging

logger = logging.getLogger(__name__)

# --- Use the definitive path from the working build script ---
CHROME_EXECUTABLE_PATH = "/opt/render/project/.render/chrome/opt/google/chrome/google-chrome"
REPO_URL = "https://github.com/dteviot/WebToEpub.git"
REPO_DIR = "webtoepub_repo"

# This JS runner now correctly loads ALL necessary dependencies first.
PARSER_RUNNER_JS = """
async ([parserScript, url, ...dependencyScripts]) => {
    try {
        let activeParserInstance = null;
        for (const script of dependencyScripts) { eval(script); }
        parserFactory.register = (domains, parser) => {
            activeParserInstance = new parser(url, document);
        };
        eval(parserScript);

        if (activeParserInstance) {
            const parser = activeParserInstance;
            if (parser.isChapterUrl && parser.isChapterUrl(url)) {
                 const contentElement = await parser.getContent();
                 return { type: 'content', html: contentElement.innerHTML };
            } else {
                const chapters = await parser.getChapters();
                const novelTitle = parser.getNovelTitle ? parser.getNovelTitle() : document.title;
                return { type: 'chapters', title: novelTitle, chapters: chapters.map(ch => ({ title: ch.title, url: ch.url })) };
            }
        }
        return { error: 'No parser was registered or activated.' };
    } catch (error) {
        return { error: `JS execution failed: ${error.toString()}` };
    }
}
"""

async def update_parsers_from_github(sent_message):
    """
    Runs as a background task, providing progress updates by editing the message.
    Crucially, runs blocking I/O (git) in a separate thread to prevent hanging.
    """
    try:
        # --- THIS IS THE FIX FOR THE HANGING ---
        # Run the blocking git operations in a separate thread.
        def git_operations():
            if os.path.exists(REPO_DIR):
                logger.info("Git repo exists. Pulling latest changes...")
                repo = git.Repo(REPO_DIR)
                origin = repo.remotes.origin
                origin.pull()
                logger.info("Pulled latest changes from WebToEpub repository.")
            else:
                logger.info("Git repo does not exist. Cloning...")
                git.Repo.clone_from(REPO_URL, REPO_DIR)
                logger.info("Cloned WebToEpub repository.")
        
        await sent_message.edit_text("Updating parsers... (Accessing repository)")
        await asyncio.to_thread(git_operations)
        await sent_message.edit_text("Updating parsers... (Repository updated, preparing to scan files)")

        js_dir = os.path.join(REPO_DIR, "plugin", "js")
        parsers_dir = os.path.join(js_dir, "parsers")
        if not os.path.isdir(parsers_dir):
            raise FileNotFoundError("Parsers directory not found after git operation.")
            
        dependency_files = ["Util.js", "Parser.js", "ParserFactory.js"]
        dependency_scripts = {}
        for file in dependency_files:
            with open(os.path.join(js_dir, file), 'r', encoding='utf-8') as f:
                dependency_scripts[file] = f.read()

        parser_files = [f for f in os.listdir(parsers_dir) if f.endswith('.js') and f != "Template.js"]
        parsers_to_save = []
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=CHROME_EXECUTABLE_PATH, args=['--no-sandbox'])
            
            total_files = len(parser_files)
            await sent_message.edit_text(f"Updating parsers... (Starting scan of {total_files} files)")
            
            for i, filename in enumerate(parser_files):
                if (i + 1) % 20 == 0: # Update every 20 files to avoid rate limits
                    try:
                        await sent_message.edit_text(f"Updating parsers... Scanned {i+1}/{total_files} files.")
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning(f"Could not edit progress message: {e}")

                page = await browser.new_page()
                try:
                    with open(os.path.join(parsers_dir, filename), 'r', encoding='utf-8') as f:
                        parser_script = f.read()
                    
                    html_content = f"""
                    <!DOCTYPE html><html><body>
                        <script>{dependency_scripts["Util.js"]}</script>
                        <script>{dependency_scripts["Parser.js"]}</script>
                        <script>{dependency_scripts["ParserFactory.js"]}</script>
                        <script>
                            let registeredDomains = [];
                            parserFactory.register = (domains, parser) => {{
                                if (typeof domains === 'string') {{ registeredDomains.push(domains); }}
                                else if (Array.isArray(domains)) {{ registeredDomains.push(...domains); }}
                            }};
                        </script>
                        <script>{parser_script}</script>
                    </body></html>
                    """
                    
                    data_url = f"data:text/html,{quote(html_content)}"
                    await page.goto(data_url, timeout=15000)
                    domains = await page.evaluate("() => window.registeredDomains")

                    if isinstance(domains, list) and domains:
                        parsers_to_save.append({ "filename": filename, "domains": domains, "script": parser_script })
                except Exception as e:
                    logger.error(f"Failed to process {filename}: {e}", exc_info=True)
                finally:
                    await page.close()
            
            await browser.close()

        if parsers_to_save:
            save_parsers_from_repo(parsers_to_save)
            count = len(parsers_to_save)
            await sent_message.edit_text(f"✅ Parser update complete. Successfully saved {count} parsers.")
        else:
            await sent_message.edit_text("ℹ️ Parser update finished, but no parsers were successfully scanned. Check logs for errors.")
            
    except Exception as e:
        logger.error("A critical error occurred during parser update:", exc_info=True)
        await sent_message.edit_text(f"❌ Parser update failed with a critical error: {e}")


async def get_chapter_list(url: str, user_id: int) -> (str, list, bool):
    if not os.path.exists(CHROME_EXECUTABLE_PATH):
        raise FileNotFoundError(f"FATAL: Chrome executable not found: {CHROME_EXECUTABLE_PATH}")
    repo_parser = get_repo_parser(url)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME_EXECUTABLE_PATH, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until='networkidle', timeout=60000)
        except Exception as e:
            await browser.close()
            raise IOError(f"Failed to navigate to URL: {e}")

        if repo_parser:
            logger.info(f"Executing parser '{repo_parser['filename']}' for {url}")
            js_dir = os.path.join(REPO_DIR, "plugin", "js")
            try:
                dependency_scripts = []
                for file in ["Util.js", "Parser.js", "ParserFactory.js"]:
                    with open(os.path.join(js_dir, file), 'r', encoding='utf-8') as f:
                        dependency_scripts.append(f.read())
                
                result = await page.evaluate(PARSER_RUNNER_JS, [repo_parser['script'], url] + dependency_scripts)
                
                if result and 'error' not in result and result.get('type') == 'chapters':
                    await browser.close()
                    logger.info(f"Parser successfully extracted {len(result['chapters'])} chapters.")
                    chapters = result['chapters']
                    for chapter in chapters:
                        chapter['url'] = urljoin(url, chapter['url'])
                        chapter['selected'] = True
                    return result['title'], chapters, True
                else:
                    logger.error(f"Parser '{repo_parser['filename']}' failed: {result.get('error', 'Unknown error')}")
            except FileNotFoundError as e:
                 logger.error(f"Could not find base parser dependency for get_chapter_list: {e.filename}")
        
        logger.warning("No parser found or parser failed. Falling back to generic scraping.")
        html_content = await page.content()
        await browser.close()
        soup = BeautifulSoup(html_content, 'html.parser')
        title = (soup.find('title').string or 'Untitled').strip()
        links = soup.find_all('a', href=True)
        chapters = [{'title': link.text.strip(), 'url': urljoin(url, link['href']), 'selected': True} for link in links if link.text.strip() and re.search(r'chapter|ep\d+|ch\.\d+', link.text.lower(), re.I)]
        
        if not chapters:
            chapters = [{'title': "Full Page Content", 'url': url, 'selected': True}]
        return title, chapters, False


async def create_epub_from_chapters(chapters: list, title: str, settings: dict) -> (str, str):
    final_filename = re.sub(r'[\\/*?:"<>|]', "", title)
    book = epub.EpubBook()
    book.set_identifier('id' + title)
    book.set_title(title)
    book.set_language('en')
    book.add_author('WebToEpub Bot')
    book_spine = ['nav']
    
    js_dir = os.path.join(REPO_DIR, "plugin", "js")
    dependency_scripts = []
    try:
        for file in ["Util.js", "Parser.js", "ParserFactory.js"]:
            with open(os.path.join(js_dir, file), 'r', encoding='utf-8') as f:
                dependency_scripts.append(f.read())
    except FileNotFoundError:
        dependency_scripts = []
        logger.error("Could not load base parser files for content extraction.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME_EXECUTABLE_PATH, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])

        for i, chapter_data in enumerate(chapters):
            if not chapter_data.get('selected', False):
                continue
            
            page = await browser.new_page()
            try:
                logger.info(f"Fetching chapter: {chapter_data['title']} ({chapter_data['url']})")
                await page.goto(chapter_data['url'], wait_until='networkidle', timeout=60000)
                
                repo_parser = get_repo_parser(chapter_data['url'])
                chapter_html_content = ''

                if repo_parser and dependency_scripts:
                    logger.info(f"Executing getContent() from '{repo_parser['filename']}'...")
                    result = await page.evaluate(PARSER_RUNNER_JS, [repo_parser['script'], chapter_data['url']] + dependency_scripts)
                    if result and 'error' not in result and result.get('type') == 'content':
                        chapter_html_content = result['html']
                    else:
                        logger.warning(f"Parser content extraction failed: {result.get('error', 'N/A')}. Falling back.")
                        chapter_html_content = await page.content()
                else:
                    chapter_html_content = await page.content()

                final_html = f"<h1>{chapter_data['title']}</h1>{chapter_html_content}"
                epub_chapter = epub.EpubHtml(title=chapter_data['title'], file_name=f'chap_{i+1}.xhtml', lang='en')
                epub_chapter.content = final_html
                book.add_item(epub_chapter)
                book_spine.append(epub_chapter)
            except Exception as e:
                logger.error(f"FATAL error processing chapter '{chapter_data['title']}': {e}", exc_info=True)
            finally:
                await page.close()
        
        await browser.close()

    book.spine = book_spine
    book.toc = [(epub.Link(c.file_name, c.title, f"chap_{i+1}")) for i, c in enumerate(book.items) if isinstance(c, epub.EpubHtml)]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub_path = f"{final_filename}.epub"
    epub.write_epub(epub_path, book, {})

    return epub_path, final_filename
