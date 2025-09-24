import asyncio
import os
import re
import httpx
from ebooklib import epub
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from database import get_custom_parser, get_repo_parser, save_parsers_from_repo
from urllib.parse import urljoin, quote
import logging

# Set up a more detailed logger
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CHROME_EXECUTABLE_PATH = "/opt/render/project/.render/chrome/opt/google/chrome/google-chrome"
REPO_DIR = "webtoepub_lib" 

def _load_dependency_scripts(as_dict=False):
    """
    Loads all dependency scripts from the local webtoepub_lib directory.
    """
    js_dir = os.path.abspath(os.path.join(REPO_DIR, "plugin", "js"))
    plugin_dir = os.path.abspath(os.path.join(REPO_DIR, "plugin"))
    unittest_dir = os.path.abspath(os.path.join(REPO_DIR, "unitTest"))

    dependency_map = {
        "_locales/en/messages.json": plugin_dir,
        "polyfillChrome.js": unittest_dir,
        "EpubItem.js": js_dir, "DebugUtil.js": js_dir, "HttpClient.js": js_dir,
        "ImageCollector.js": js_dir, "Imgur.js": js_dir, "Parser.js": js_dir,
        "ParserFactory.js": js_dir, "UserPreferences.js": js_dir, "Util.js": js_dir,
    }

    scripts = {} if as_dict else []
    for file, base_dir in dependency_map.items():
        filepath = os.path.join(base_dir, file)
        try:
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
        except FileNotFoundError:
            logger.error(f"FATAL: A required library file was not found: {filepath}")
            # Return an empty list/dict to indicate failure
            return {} if as_dict else []
    return scripts


async def update_parsers_from_github(sent_message=None):
    total_saved_count = 0
    API_URL = "https://api.github.com/repos/dteviot/WebToEpub/contents/plugin/js/parsers"
    
    async def _log(message):
        if sent_message:
            await sent_message.edit_text(message)
        else:
            logger.info(f"Parser Update Status: {message}")

    try:
        await _log("Fetching parser list from GitHub...")
        async with httpx.AsyncClient() as client:
            response = await client.get(API_URL)
            response.raise_for_status()
            parser_files_meta = response.json()

        parser_files = [f for f in parser_files_meta if f['name'].endswith('.js') and f['name'] != 'Template.js']
        total_files = len(parser_files)
        
        await _log(f"Found {total_files} parsers. Processing...")
        dependency_scripts = _load_dependency_scripts(as_dict=True)
        
        if not dependency_scripts:
            await _log("❌ Critical Error: Could not load base dependency scripts. Aborting update.")
            return

        BATCH_SIZE = 50 
        for i in range(0, total_files, BATCH_SIZE):
            batch_meta = parser_files[i:i + BATCH_SIZE]
            batch_to_save = []
            
            await _log(f"Processing batch {i//BATCH_SIZE + 1}/{(total_files + BATCH_SIZE - 1)//BATCH_SIZE}...")
            
            async with async_playwright() as p, httpx.AsyncClient() as client:
                browser = await p.chromium.launch(executable_path=CHROME_EXECUTABLE_PATH, args=['--no-sandbox'])
                page = await browser.new_page()

                for meta in batch_meta:
                    try:
                        script_res = await client.get(meta['download_url'])
                        script_res.raise_for_status()
                        parser_script = script_res.text

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
                            batch_to_save.append({ "filename": meta['name'], "domains": domains, "script": parser_script })
                    except Exception as e:
                        logger.error(f"Failed to process {meta['name']}: {e}", exc_info=False)
                
                await browser.close()

            if batch_to_save:
                saved_in_batch = save_parsers_from_repo(batch_to_save)
                total_saved_count += saved_in_batch
                await _log(f"Saved {total_saved_count}/{total_files} parsers so far...")

        await _log(f"✅ Parser update complete. Successfully saved {total_saved_count}/{total_files} parsers.")
            
    except Exception as e:
        logger.error("A critical error occurred during parser update:", exc_info=True)
        await _log(f"❌ Parser update failed with a critical error: {e}")


async def get_chapter_list(url: str, user_id: int):
    logger.info(f"Starting chapter list fetch for URL: {url}")
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
            logger.info(f"Found specific parser: {repo_parser['filename']}")
            try:
                dependency_scripts = _load_dependency_scripts()
                if not dependency_scripts:
                     raise FileNotFoundError("Could not load dependency scripts for parser execution.")

                for script_content in dependency_scripts:
                    await page.add_script_tag(content=script_content)

                result = await page.evaluate("""
                    async (parserScript) => {
                        try {
                            let activeParserInstance = null;
                            parserFactory.register = (domains, parser) => {
                                activeParserInstance = new parser(document.URL, document);
                            };
                            eval(parserScript);
                            
                            if (activeParserInstance) {
                                const parser = activeParserInstance;
                                if (parser.isChapterUrl && parser.isChapterUrl(document.URL)) {
                                    const novelTitle = parser.getNovelTitle ? parser.getNovelTitle() : document.title;
                                    const chapterTitle = parser.getChapterTitle ? parser.getChapterTitle() : document.title;
                                    return {
                                        type: 'chapters',
                                        title: novelTitle,
                                        chapters: [{ title: chapterTitle, url: document.URL }]
                                    };
                                } else {
                                    const chapters = await parser.getChapters();
                                    const novelTitle = parser.getNovelTitle ? parser.getNovelTitle() : document.title;
                                    return { type: 'chapters', title: novelTitle, chapters: chapters.map(ch => ({ title: ch.title, url: ch.url })) };
                                }
                            }
                            return { error: 'No parser instance was registered or activated by the script.' };
                        } catch (error) {
                            return { error: `JavaScript execution crashed: ${error.toString()}`, stack: error.stack };
                        }
                    }
                """, repo_parser['script'])
                
                logger.info(f"JavaScript execution result: {result}")
                
                if result and 'error' not in result and result.get('type') == 'chapters' and result.get('chapters'):
                    await browser.close()
                    logger.info(f"Successfully parsed {len(result['chapters'])} chapters for title: {result['title']}")
                    chapters = result['chapters']
                    for chapter in chapters:
                        chapter['url'] = urljoin(url, chapter['url'])
                        chapter['selected'] = True
                    return result['title'], chapters, True
                else:
                    # THIS IS THE NEW, CRITICAL LOGGING
                    error_details = result.get('error', 'Unknown error') if result else 'No result object returned'
                    logger.error(f"Parser '{repo_parser['filename']}' failed to return valid chapter data. Reason: {error_details}")
            except Exception as e:
                logger.error(f"A Python-level exception occurred while using specific parser: {e}", exc_info=True)

        logger.warning("No specific parser was used or it failed. Falling back to generic scraping.")
        html_content = await page.content()
        await browser.close()
        soup = BeautifulSoup(html_content, 'html.parser')
        title = (soup.find('title').string or 'Untitled').strip()
        links = soup.find_all('a', href=True)
        chapters = [{'title': link.text.strip(), 'url': urljoin(url, link['href']), 'selected': True} for link in links if link.text.strip() and re.search(r'chapter|ep\d+|ch\.\d+', link.text.lower(), re.I)]
        if not chapters:
            chapters = [{'title': "Full Page Content", 'url': url, 'selected': True}]
        logger.info(f"Generic scraping found {len(chapters)} potential chapters.")
        return title, chapters, False


async def create_epub_from_chapters(chapters: list, title: str, settings: dict):
    final_filename = re.sub(r'[\\/*?:"<>|]', "", title)
    book = epub.EpubBook()
    book.set_identifier('id' + title)
    book.set_title(title)
    book.set_language('en')
    book.add_author('WebToEpub Bot')
    book_spine = ['nav']
    
    dependency_scripts = _load_dependency_scripts()
    if not dependency_scripts:
        logger.error("Cannot create EPUB: Failed to load dependency scripts.")
        return None, None

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME_EXECUTABLE_PATH, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
        for i, chapter_data in enumerate(chapters):
            if not chapter_data.get('selected', False): continue
            page = await browser.new_page()
            try:
                await page.goto(chapter_data['url'], wait_until='networkidle', timeout=60000)
                repo_parser = get_repo_parser(chapter_data['url'])
                chapter_html_content = ''
                if repo_parser:
                    for script_content in dependency_scripts:
                        await page.add_script_tag(content=script_content)
                    
                    result = await page.evaluate("""
                        async (parserScript) => {
                            try {
                                let activeParserInstance = null;
                                parserFactory.register = (domains, parser) => {
                                    activeParserInstance = new parser(document.URL, document);
                                };
                                eval(parserScript);
                                if (activeParserInstance && activeParserInstance.isChapterUrl && activeParserInstance.isChapterUrl(document.URL)) {
                                    const contentElement = await activeParserInstance.getContent();
                                    return { type: 'content', html: contentElement.innerHTML };
                                }
                                return { error: 'Parser did not identify this as a chapter URL or failed to get content.' };
                            } catch (error) {
                                return { error: `JavaScript execution crashed: ${error.toString()}`, stack: error.stack };
                            }
                        }
                    """, repo_parser['script'])

                    if result and 'error' not in result and result.get('type') == 'content':
                        chapter_html_content = result['html']
                    else:
                        error_details = result.get('error', 'Unknown error') if result else 'No result object'
                        logger.error(f"Parser '{repo_parser['filename']}' failed to get content for '{chapter_data['title']}'. Reason: {error_details}. Falling back.")
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
