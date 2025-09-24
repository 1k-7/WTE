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

CHROME_EXECUTABLE_PATH = "/opt/render/project/.render/chrome/opt/google/chrome/google-chrome"
REPO_URL = "https://github.com/dteviot/WebToEpub.git"
REPO_DIR = "webtoepub_repo"

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
        return { error: `JavaScript execution failed: ${error.toString()}` };
    }
}
"""

def _load_dependency_scripts(as_dict=False):
    """
    Loads all dependency scripts from the repository with robust path handling.
    """
    js_dir = os.path.join(REPO_DIR, "plugin", "js")
    plugin_dir = os.path.join(REPO_DIR, "plugin")
    unittest_dir = os.path.join(REPO_DIR, "unitTest")

    # Define files and their correct base directories
    dependency_map = {
        "../_locales/en/messages.json": plugin_dir,
        "polyfillChrome.js": unittest_dir,
        "EpubItem.js": js_dir, "DebugUtil.js": js_dir, "HttpClient.js": js_dir,
        "ImageCollector.js": js_dir, "Imgur.js": js_dir, "Parser.js": js_dir,
        "ParserFactory.js": js_dir, "UserPreferences.js": js_dir, "Util.js": js_dir,
    }

    scripts = {} if as_dict else []
    for file, base_dir in dependency_map.items():
        # Construct the path safely, handling the '..' case explicitly
        path_segments = file.replace('../', '').split('/')
        filepath = os.path.join(base_dir, *path_segments)
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            if file.endswith('.json'):
                script_content = f'const messages = {content};'
            else:
                script_content = content
            
            if as_dict:
                scripts[file] = script_content
            else:
                scripts.append(script_content)
    return scripts


async def update_parsers_from_github(sent_message, limit=None):
    total_saved_count = 0
    try:
        def git_operations():
            if os.path.exists(REPO_DIR):
                repo = git.Repo(REPO_DIR); origin = repo.remotes.origin; origin.pull()
            else:
                git.Repo.clone_from(REPO_URL, REPO_DIR)
        
        await sent_message.edit_text("Updating parsers... (Accessing repository)")
        await asyncio.to_thread(git_operations)
        
        js_dir = os.path.join(REPO_DIR, "plugin", "js")
        parsers_dir = os.path.join(js_dir, "parsers")
        if not os.path.isdir(parsers_dir): raise FileNotFoundError("Parsers directory not found.")
            
        dependency_scripts = _load_dependency_scripts(as_dict=True)

        parser_files = [f for f in os.listdir(parsers_dir) if f.endswith('.js') and f != "Template.js"]
        if limit:
            parser_files = parser_files[:limit]
        total_files = len(parser_files)
        
        BATCH_SIZE = 50 
        for i in range(0, total_files, BATCH_SIZE):
            batch = parser_files[i:i + BATCH_SIZE]
            batch_to_save = []
            
            await sent_message.edit_text(f"Updating parsers... (Processing batch {i//BATCH_SIZE + 1}/{(total_files + BATCH_SIZE - 1)//BATCH_SIZE})")
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(executable_path=CHROME_EXECUTABLE_PATH, args=['--no-sandbox'])
                page = await browser.new_page()

                for filename in batch:
                    try:
                        with open(os.path.join(parsers_dir, filename), 'r', encoding='utf-8') as f:
                            parser_script = f.read()
                        
                        script_tags = "".join([f"<script>{s}</script>" for s in dependency_scripts.values()])
                        html_content = f"""
                        <!DOCTYPE html><html><body>
                            {script_tags}
                            <script>
                                var registeredDomains = [];
                                parserFactory.register = (domains, parser) => {{
                                    if (typeof domains === 'string') {{ registeredDomains.push(domains); }}
                                    else if (Array.isArray(domains)) {{ registeredDomains.push(...domains); }}
                                }};
                            </script>
                            <script>{parser_script}</script>
                        </body></html>
                        """
                        data_url = f"data:text/html,{quote(html_content)}"
                        await page.goto(data_url, timeout=15000, wait_until='domcontentloaded')
                        domains = await page.evaluate("() => window.registeredDomains")

                        if isinstance(domains, list) and domains:
                            batch_to_save.append({ "filename": filename, "domains": domains, "script": parser_script })
                    except Exception as e:
                        logger.error(f"Failed to process {filename}: {e}", exc_info=False)
                
                await browser.close()
            
            if batch_to_save:
                saved_in_batch = save_parsers_from_repo(batch_to_save)
                total_saved_count += saved_in_batch
                await sent_message.edit_text(f"Updating parsers... (Saved {total_saved_count}/{total_files} parsers so far)")
                if i == 0 and saved_in_batch == 0:
                     await sent_message.edit_text("❌ First batch failed to save any parsers. The update process is flawed. Please check logs.")
                     return
            await asyncio.sleep(1)

        await sent_message.edit_text(f"✅ Parser update complete. Successfully saved {total_saved_count}/{total_files} parsers.")
            
    except Exception as e:
        logger.error("A critical error occurred during parser update:", exc_info=True)
        await sent_message.edit_text(f"❌ Parser update failed with a critical error: {e}")


async def get_chapter_list(url: str, user_id: int):
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
            try:
                dependency_scripts = _load_dependency_scripts()
                result = await page.evaluate(PARSER_RUNNER_JS, [repo_parser['script'], url] + dependency_scripts)
                
                if result and 'error' not in result and result.get('type') == 'chapters':
                    await browser.close()
                    chapters = result['chapters']
                    for chapter in chapters:
                        chapter['url'] = urljoin(url, chapter['url'])
                        chapter['selected'] = True
                    return result['title'], chapters, True
                else:
                    logger.error(f"Parser '{repo_parser['filename']}' failed: {result.get('error', 'Unknown error')}")
            except FileNotFoundError as e:
                 logger.error(f"Could not find base parser dependency for get_chapter_list: {e.filename}", exc_info=True)
        
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


async def create_epub_from_chapters(chapters: list, title: str, settings: dict):
    final_filename = re.sub(r'[\\/*?:"<>|]', "", title)
    book = epub.EpubBook()
    book.set_identifier('id' + title)
    book.set_title(title)
    book.set_language('en')
    book.add_author('WebToEpub Bot')
    book_spine = ['nav']
    
    dependency_scripts = []
    try:
        dependency_scripts = _load_dependency_scripts()
    except FileNotFoundError:
        logger.error("Could not load dependency scripts for EPUB creation.", exc_info=True)
        dependency_scripts = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME_EXECUTABLE_PATH, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
        for i, chapter_data in enumerate(chapters):
            if not chapter_data.get('selected', False): continue
            page = await browser.new_page()
            try:
                await page.goto(chapter_data['url'], wait_until='networkidle', timeout=60000)
                repo_parser = get_repo_parser(chapter_data['url'])
                chapter_html_content = ''
                if repo_parser and dependency_scripts:
                    result = await page.evaluate(PARSER_RUNNER_JS, [repo_parser['script'], chapter_data['url']] + dependency_scripts)
                    if result and 'error' not in result and result.get('type') == 'content':
                        chapter_html_content = result['html']
                    else:
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
