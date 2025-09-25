import asyncio
import os
import re
from ebooklib import epub
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from database import get_custom_parser, get_repo_parser, save_parsers_from_repo, clean_all_parsers, get_parser_count
from urllib.parse import urljoin, quote
import logging
import json

# Enhanced logging to capture every detail
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(funcName)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CHROME_EXECUTABLE_PATH = "/opt/render/project/.render/chrome/opt/google/chrome/google-chrome"
REPO_DIR = "webtoepub_lib" 

# A global flag to ensure we only check the DB once per session
PARSERS_LOADED = False

def _load_dependency_scripts():
    js_dir = os.path.abspath(os.path.join(REPO_DIR, "plugin", "js"))
    plugin_dir = os.path.abspath(os.path.join(REPO_DIR, "plugin"))
    unittest_dir = os.path.abspath(os.path.join(REPO_DIR, "unitTest"))

    dependency_map = {
        "_locales/en/messages.json": plugin_dir, "polyfillChrome.js": unittest_dir,
        "EpubItem.js": js_dir, "DebugUtil.js": js_dir, "HttpClient.js": js_dir,
        "ImageCollector.js": js_dir, "Imgur.js": js_dir, "Parser.js": js_dir,
        "ParserFactory.js": js_dir, "UserPreferences.js": js_dir, "Util.js": js_dir,
    }

    scripts = []
    for file, base_dir in dependency_map.items():
        filepath = os.path.join(base_dir, file)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                script_content = f'const messages = {content};' if file.endswith('.json') else content
                scripts.append(script_content)
        except FileNotFoundError:
            logger.error(f"FATAL: A required library file was not found: {filepath}")
            return []
    return scripts

async def generate_parsers_manifest(sent_message):
    """
    Scans local parser files, extracts their domains using Playwright,
    and writes the result to parsers.json.
    """
    await sent_message.edit_text("Starting parser scan... This is a one-time process and will take several minutes.")
    
    parsers_dir = os.path.abspath(os.path.join(REPO_DIR, "plugin", "js", "parsers"))
    manifest_path = 'parsers.json'
    
    if not os.path.isdir(parsers_dir):
        await sent_message.edit_text(f"❌ ERROR: Parsers directory not found at {parsers_dir}.")
        return

    dependency_scripts_list = _load_dependency_scripts()
    if not dependency_scripts_list:
        await sent_message.edit_text("❌ ERROR: Could not load base dependency scripts. Aborting.")
        return
        
    parser_files = [f for f in os.listdir(parsers_dir) if f.endswith('.js') and f != 'Template.js']
    total_files = len(parser_files)
    logger.info(f"Found {total_files} local parser files to process for manifest.")
    
    manifest_data = {}
    processed_count = 0
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME_EXECUTABLE_PATH, args=['--no-sandbox'])
        page = await browser.new_page()

        for filename in parser_files:
            processed_count += 1
            if processed_count % 10 == 0:
                await sent_message.edit_text(f"Scanning parsers... ({processed_count}/{total_files})")
            
            try:
                filepath = os.path.join(parsers_dir, filename)
                with open(filepath, 'r', encoding='utf-8') as f:
                    parser_script = f.read()

                script_tags = "".join([f"<script>{s}</script>" for s in dependency_scripts_list])
                html_content = f"<!DOCTYPE html><html><body>{script_tags}<script>var registeredDomains = []; parserFactory.register = (domains, parser) => {{ if (typeof domains === 'string') {{ registeredDomains.push(domains); }} else if (Array.isArray(domains)) {{ registeredDomains.push(...domains); }} }};</script><script>{parser_script}</script></body></html>"
                data_url = f"data:text/html,{quote(html_content)}"
                await page.goto(data_url, timeout=15000, wait_until='domcontentloaded')
                domains = await page.evaluate("() => window.registeredDomains")

                if isinstance(domains, list) and domains:
                    manifest_data[filename] = domains
                    logger.info(f"Extracted domains for {filename}: {domains}")
                else:
                    logger.warning(f"No domains found for {filename}")

            except Exception as e:
                logger.error(f"Failed to process local parser {filename} for manifest: {e}", exc_info=False)
        
        await browser.close()
    
    if manifest_data:
        try:
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest_data, f, indent=2)
            await sent_message.edit_text(f"✅ Success! `parsers.json` has been generated with {len(manifest_data)} entries. You can now use the bot.")
        except Exception as e:
            await sent_message.edit_text(f"❌ ERROR: Could not write to `{manifest_path}`. Reason: {e}")
    else:
        await sent_message.edit_text("⚠️ Warning: No parsers were successfully processed. `parsers.json` was not created.")

async def load_parsers_from_manifest():
    """
    Loads parsers from the local parsers.json file into the database.
    This version is designed to fail loudly if there's a problem.
    """
    global PARSERS_LOADED
    logger.info("Starting parser load from manifest...")
    manifest_path = 'parsers.json'
    parsers_dir = os.path.abspath(os.path.join(REPO_DIR, "plugin", "js", "parsers"))
    
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except FileNotFoundError:
        logger.error(f"FATAL: `parsers.json` not found. Run /parserjson to create it.")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"FATAL: `parsers.json` is not valid JSON. Please fix or recreate it. Error: {e}")
        raise
    
    parsers_to_save = []
    for filename, domains in manifest.items():
        filepath = os.path.join(parsers_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                script_content = f.read()
            parsers_to_save.append({
                "filename": filename,
                "domains": domains,
                "script": script_content
            })
        except FileNotFoundError:
            logger.error(f"Parser file '{filename}' from manifest not found at '{filepath}'.")

    if parsers_to_save:
        clean_all_parsers()
        saved_count = save_parsers_from_repo(parsers_to_save)
        logger.info(f"✅ Successfully loaded {saved_count}/{len(parsers_to_save)} parsers from manifest into the database.")
        PARSERS_LOADED = True
    else:
        logger.warning("No parsers were loaded from the manifest. The database may be empty.")

async def ensure_parsers_are_loaded():
    """
    Checks if parsers are loaded in the DB. If not, loads them from the manifest.
    This is called before the /epub command runs.
    """
    global PARSERS_LOADED
    if not PARSERS_LOADED:
        count = get_parser_count()
        if count == 0:
            logger.info("Parser database is empty. Loading from manifest for the first time this session.")
            await load_parsers_from_manifest()
        else:
            logger.info(f"{count} parsers already in database. Skipping load.")
            PARSERS_LOADED = True

async def load_parsers_from_json_content(json_content, sent_message):
    """
    Loads parsers from a JSON string into the database.
    """
    global PARSERS_LOADED
    logger.info("Loading parsers from provided JSON content...")
    parsers_dir = os.path.abspath(os.path.join(REPO_DIR, "plugin", "js", "parsers"))
    
    try:
        manifest = json.loads(json_content)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON provided: {e}")
        await sent_message.edit_text("❌ ERROR: The provided file is not valid JSON.")
        return

    parsers_to_save = []
    for filename, domains in manifest.items():
        filepath = os.path.join(parsers_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                script_content = f.read()
            parsers_to_save.append({
                "filename": filename,
                "domains": domains,
                "script": script_content
            })
        except FileNotFoundError:
            logger.error(f"Parser file '{filename}' from manifest not found at '{filepath}'.")

    if parsers_to_save:
        clean_all_parsers()
        saved_count = save_parsers_from_repo(parsers_to_save)
        logger.info(f"✅ Successfully loaded {saved_count}/{len(parsers_to_save)} parsers into the database.")
        await sent_message.edit_text(f"✅ Success! Loaded {saved_count} parsers into the database.")
        PARSERS_LOADED = True
    else:
        logger.warning("No parsers were successfully loaded from the provided JSON.")
        await sent_message.edit_text("⚠️ Warning: No parsers were loaded. Please check the file content.")


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
