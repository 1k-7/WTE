import asyncio
import os
import re
from ebooklib import epub
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from database import get_custom_parser, get_repo_parser, save_parsers_from_repo, clean_all_parsers
from urllib.parse import urljoin, quote
import logging
import json

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CHROME_EXECUTABLE_PATH = "/opt/render/project/.render/chrome/opt/google/chrome/google-chrome"
REPO_DIR = "webtoepub_lib" 

def _load_dependency_scripts(as_dict=False):
    js_dir = os.path.abspath(os.path.join(REPO_DIR, "plugin", "js"))
    plugin_dir = os.path.abspath(os.path.join(REPO_DIR, "plugin"))
    unittest_dir = os.path.abspath(os.path.join(REPO_DIR, "unitTest"))

    dependency_map = {
        "_locales/en/messages.json": plugin_dir, "polyfillChrome.js": unittest_dir,
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
                script_content = f'const messages = {content};' if file.endswith('.json') else content
                if as_dict:
                    scripts[file] = script_content
                else:
                    scripts.append(script_content)
        except FileNotFoundError:
            logger.error(f"FATAL: A required library file was not found: {filepath}")
            return {} if as_dict else []
    return scripts

async def load_local_parsers_to_db():
    """
    Loads all local parser files from the webtoepub_lib/plugin/js/parsers directory
    into the MongoDB database. This runs on startup.
    """
    logger.info("Starting local parser loading process...")
    parsers_dir = os.path.abspath(os.path.join(REPO_DIR, "plugin", "js", "parsers"))
    
    if not os.path.isdir(parsers_dir):
        logger.error(f"Parsers directory not found at {parsers_dir}. Cannot load parsers.")
        return

    dependency_scripts = _load_dependency_scripts(as_dict=True)
    if not dependency_scripts:
        logger.error("Could not load base dependency scripts. Aborting parser load.")
        return
        
    parser_files = [f for f in os.listdir(parsers_dir) if f.endswith('.js')]
    total_files = len(parser_files)
    logger.info(f"Found {total_files} local parser files to process.")

    batch_to_save = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME_EXECUTABLE_PATH, args=['--no-sandbox'])
        page = await browser.new_page()

        for filename in parser_files:
            try:
                filepath = os.path.join(parsers_dir, filename)
                with open(filepath, 'r', encoding='utf-8') as f:
                    parser_script = f.read()

                script_tags = "".join([f"<script>{s}</script>" for s in dependency_scripts.values()])
                html_content = f"<!DOCTYPE html><html><body>{script_tags}<script>var registeredDomains = []; parserFactory.register = (domains, parser) => {{ if (typeof domains === 'string') {{ registeredDomains.push(domains); }} else if (Array.isArray(domains)) {{ registeredDomains.push(...domains); }} }};</script><script>{parser_script}</script></body></html>"
                data_url = f"data:text/html,{quote(html_content)}"
                await page.goto(data_url, timeout=15000, wait_until='domcontentloaded')
                domains = await page.evaluate("() => window.registeredDomains")

                if isinstance(domains, list) and domains:
                    batch_to_save.append({ "filename": filename, "domains": domains, "script": parser_script })
            except Exception as e:
                logger.error(f"Failed to process local parser {filename}: {e}", exc_info=False)
        
        await browser.close()
    
    if batch_to_save:
        clean_all_parsers()
        saved_count = save_parsers_from_repo(batch_to_save)
        logger.info(f"✅ Successfully loaded {saved_count}/{len(batch_to_save)} parsers into the database.")
    else:
        logger.warning("No parsers were successfully processed and loaded.")


async def run_parser_in_browser(page, parser_script, task_type):
    dependency_scripts = _load_dependency_scripts()
    if not dependency_scripts:
        raise FileNotFoundError("Could not load dependency scripts for parser execution.")

    loop = asyncio.get_running_loop()
    future = loop.create_future()

    async def set_result(result_json):
        if not future.done():
            future.set_result(json.loads(result_json))

    await page.expose_function("sendResultToPython", set_result)

    for script_content in dependency_scripts:
        await page.add_script_tag(content=script_content)
    
    await page.evaluate("""
        async ([parserScript, task]) => {
            let result = { error: 'Unknown execution error' };
            try {
                let activeParserInstance = null;
                parserFactory.register = (domains, parser) => {
                    activeParserInstance = new parser(document.URL, document);
                };
                eval(parserScript);

                if (activeParserInstance) {
                    const parser = activeParserInstance;
                    if (task === 'getChapters') {
                        if (parser.isChapterUrl && parser.isChapterUrl(document.URL)) {
                            const novelTitle = parser.getNovelTitle ? parser.getNovelTitle() : document.title;
                            const chapterTitle = parser.getChapterTitle ? parser.getChapterTitle() : document.title;
                            result = { type: 'chapters', title: novelTitle, chapters: [{ title: chapterTitle, url: document.URL }] };
                        } else {
                            const chapters = await parser.getChapters();
                            const novelTitle = parser.getNovelTitle ? parser.getNovelTitle() : document.title;
                            result = { type: 'chapters', title: novelTitle, chapters: chapters.map(ch => ({ title: ch.title, url: ch.url })) };
                        }
                    } else if (task === 'getContent') {
                        if (parser.isChapterUrl && parser.isChapterUrl(document.URL)) {
                            const contentElement = await parser.getContent();
                            result = { type: 'content', html: contentElement.innerHTML };
                        } else {
                            result = { error: 'Parser did not identify this as a chapter URL.' };
                        }
                    }
                } else {
                    result = { error: 'No parser instance was registered or activated.' };
                }
            } catch (error) {
                result = { error: `JavaScript execution crashed: ${error.toString()}`, stack: error.stack };
            }
            window.sendResultToPython(JSON.stringify(result));
        }
    """, [parser_script, task_type])

    return await asyncio.wait_for(future, timeout=30.0)


async def get_chapter_list(url: str, user_id: int):
    logger.info(f"Starting chapter list fetch for URL: {url}")
    if not os.path.exists(CHROME_EXECUTABLE_PATH):
        raise FileNotFoundError(f"FATAL: Chrome executable not found at {CHROME_EXECUTABLE_PATH}")
    
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
                result = await run_parser_in_browser(page, repo_parser['script'], 'getChapters')
                logger.info(f"Parser execution result: {result}")
                
                if result and 'error' not in result and result.get('type') == 'chapters' and result.get('chapters'):
                    await browser.close()
                    logger.info(f"Successfully parsed {len(result['chapters'])} chapters for title: '{result['title']}'")
                    chapters = result['chapters']
                    for chapter in chapters:
                        chapter['url'] = urljoin(url, chapter['url'])
                        chapter['selected'] = True
                    return result['title'], chapters, True
                else:
                    error_details = result.get('error', 'Unknown error') if result else 'No result object returned'
                    logger.error(f"Parser '{repo_parser['filename']}' failed. Reason: {error_details}")

            except Exception as e:
                logger.error(f"A Python-level exception occurred while running the parser: {e}", exc_info=True)

        logger.warning("Parser failed or not found. Falling back to generic scraping.")
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
    book.set_identifier('id' + title); book.set_title(title); book.set_language('en'); book.add_author('WebToEpub Bot')
    book_spine = ['nav']

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
                    try:
                        result = await run_parser_in_browser(page, repo_parser['script'], 'getContent')
                        if result and 'error' not in result and result.get('type') == 'content':
                            chapter_html_content = result['html']
                        else:
                            error_details = result.get('error', 'Unknown error') if result else 'No result object'
                            logger.error(f"Parser '{repo_parser['filename']}' failed to get content for '{chapter_data['title']}'. Reason: {error_details}. Falling back.")
                            chapter_html_content = await page.content()
                    except Exception as e:
                        logger.error(f"Python-level exception getting content for '{chapter_data['title']}': {e}", exc_info=True)
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
    book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
    epub_path = f"{final_filename}.epub"
    epub.write_epub(epub_path, book, {})
    return epub_path, final_filename
