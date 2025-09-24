#w
import logging
import os
import asyncio
import re
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackContext, CallbackQueryHandler, ConversationHandler
)

from settings import (
    get_user_settings, handle_settings_callback,
    SETTING_VALUE, handle_setting_value_input, get_main_settings_menu
)
from parser import get_chapter_list, create_epub_from_chapters, update_parsers_from_github
from database import add_custom_parser, clean_repo_parsers

# --- Enable logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment variables & Constants ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
PORT = int(os.environ.get('PORT', 8080))

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")
if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL environment variable is not set. This is required for webhook mode.")

# --- Conversation states ---
TARGET_URL, PARSER_FILE = range(2)
CHAPTER_SELECTION = 0


# --- Command Handlers ---

async def start(update: Update, context: CallbackContext) -> None:
    """Send a welcome message with updated instructions."""
    await update.message.reply_text(
        'Welcome to the WebToEpub Bot!\n\n'
        'Use /epub <url> to convert a page.\n'
        'Use /settings to configure options.\n'
        'Use /update_parsers to fetch all parsers.\n'
        'Use /load <number> to fetch a specific number of parsers for testing.\n'
        'Use /cleanparsers to clear the parser database.\n'
        'Use /add_parser to add a custom parser.'
    )

async def settings_command(update: Update, context: CallbackContext) -> None:
    """Display the main settings menu."""
    user_id = update.message.from_user.id
    reply_markup, message = await get_main_settings_menu(user_id)
    await update.message.reply_text(message, reply_markup=reply_markup)

# --- REWORKED UPDATE COMMANDS ---

async def update_parsers_command(update: Update, context: CallbackContext) -> None:
    """Starts the full parser update process in the background."""
    sent_message = await update.message.reply_text("Parser update started... This may take several minutes.")
    asyncio.create_task(update_parsers_from_github(sent_message))

async def load_parsers_command(update: Update, context: CallbackContext) -> None:
    """Starts a partial parser update process for testing."""
    try:
        limit = int(context.args[0])
        sent_message = await update.message.reply_text(f"Starting partial parser load of {limit} files...")
        asyncio.create_task(update_parsers_from_github(sent_message, limit=limit))
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /load <number>")


async def clean_parsers_command(update: Update, context: CallbackContext) -> None:
    """Clears the repository parsers from the database."""
    await update.message.reply_text("Cleaning parser database...")
    try:
        deleted_count = clean_repo_parsers()
        await update.message.reply_text(f"Successfully deleted {deleted_count} parsers. You can now run /update_parsers or /load.")
    except Exception as e:
        logger.error(f"Error cleaning parsers: {e}", exc_info=True)
        await update.message.reply_text("An error occurred while cleaning the parser database.")

async def add_parser_start(update: Update, context: CallbackContext) -> int:
    """Start the custom parser upload process."""
    await update.message.reply_text("Please provide the target URL for this parser.")
    return TARGET_URL

async def received_target_url(update: Update, context: CallbackContext) -> int:
    context.user_data['target_url'] = update.message.text
    await update.message.reply_text("Thank you. Now, please upload the .js parser file.")
    return PARSER_FILE

async def received_parser_file(update: Update, context: CallbackContext) -> int:
    user_id = update.message.from_user.id
    target_url = context.user_data.get('target_url')

    if not update.message.document or not update.message.document.file_name.endswith('.js'):
        await update.message.reply_text("That's not a .js file. Please upload a valid parser file.")
        return PARSER_FILE

    try:
        file = await context.bot.get_file(update.message.document.file_id)
        parser_content = (await file.download_as_bytearray()).decode('utf-8')
        add_custom_parser(user_id, target_url, parser_content)
        await update.message.reply_text(f"Custom parser for {target_url} has been added successfully!")
    except Exception as e:
        logger.error(f"Failed to add custom parser: {e}")
        await update.message.reply_text("There was an error saving your parser.")
    return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.edit_message_text('Operation cancelled.')
    else:
        await update.message.reply_text('Operation cancelled.')
    return ConversationHandler.END


# --- Chapter Selection and EPUB Creation (No changes) ---
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def build_chapter_selection_keyboard(chapters, page=0, page_size=10):
    keyboard = []
    start_index = page * page_size
    end_index = start_index + page_size
    
    for i, chapter in enumerate(chapters[start_index:end_index]):
        status_emoji = "✅" if chapter.get('selected', True) else "❌"
        keyboard.append([InlineKeyboardButton(f"{status_emoji} {chapter['title']}", callback_data=f"toggle_chapter_{start_index + i}")])
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page-1}"))
    if end_index < len(chapters):
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}"))
    if nav_row: keyboard.append(nav_row)
    keyboard.append([
        InlineKeyboardButton("Select All", callback_data="select_all"),
        InlineKeyboardButton("Deselect All", callback_data="deselect_all")
    ])
    keyboard.append([InlineKeyboardButton("Done ✅", callback_data="done_selecting")])
    return InlineKeyboardMarkup(keyboard)

async def display_chapter_selection(update: Update, context: CallbackContext, message_text: str):
    chapters = context.user_data['chapters']
    page = context.user_data.get('page', 0)
    reply_markup = await build_chapter_selection_keyboard(chapters, page)
    if update.callback_query:
        await update.callback_query.edit_message_text(text=message_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text=message_text, reply_markup=reply_markup)

async def chapter_selection_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    action, _, data = query.data.partition('_')
    chapters = context.user_data.get('chapters', [])
    if action == 'toggle':
        chapters[int(data)]['selected'] = not chapters[int(data)].get('selected', True)
    elif action == 'page':
        context.user_data['page'] = int(data)
    elif action == 'select':
        for chapter in chapters: chapter['selected'] = (data == 'all')
    await display_chapter_selection(update, context, f"Select chapters for: {context.user_data['title']}")
    if action == 'done':
        await query.edit_message_text("Processing selected chapters...")
        selected_chapters = [ch for ch in chapters if ch.get('selected', True)]
        if not selected_chapters:
            await query.message.reply_text("No chapters selected.")
            return ConversationHandler.END
        await process_chapters_to_epub(update, context, selected_chapters)
        return ConversationHandler.END

async def process_chapters_to_epub(update: Update, context: CallbackContext, chapters: list):
    chat_id = update.effective_chat.id
    title = context.user_data.get('title', 'Untitled')
    await context.bot.send_message(chat_id, f"Creating EPUB for '{title}'...")
    try:
        user_settings = get_user_settings(update.effective_user.id)
        epub_path, final_filename = await create_epub_from_chapters(chapters, title, user_settings)
        if epub_path and os.path.exists(epub_path):
            with open(epub_path, 'rb') as epub_file:
                await context.bot.send_document(chat_id=chat_id, document=epub_file, filename=f"{final_filename}.epub", caption=f"EPUB for: {title}")
            os.remove(epub_path)
        else:
            await context.bot.send_message(chat_id, f"Failed to create EPUB for: {title}")
    except Exception as e:
        logger.error(f"Error creating EPUB: {e}", exc_info=True)
        await context.bot.send_message(chat_id, f"An error occurred: {e}")

async def epub_command(update: Update, context: CallbackContext) -> int:
    url = ""
    if context.args: url = context.args[0]
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        urls = re.findall(r'http[s]?://\S+', update.message.reply_to_message.text)
        if urls: url = urls[0]
    if not url:
        await update.message.reply_text("Usage: /epub <URL> or reply to a message with a link.")
        return ConversationHandler.END
    await update.message.reply_text(f"Fetching chapters from: {url}")
    try:
        title, chapters, parser_found = await get_chapter_list(url, update.effective_user.id)
        if not chapters: raise ValueError("No chapters found.")
        context.user_data.update({'chapters': chapters, 'title': title, 'page': 0})
        if len(chapters) == 1 and not parser_found:
            await process_chapters_to_epub(update, context, chapters)
            return ConversationHandler.END
        if not parser_found:
            keyboard = [[InlineKeyboardButton("Yes", callback_data="dp_yes"), InlineKeyboardButton("No", callback_data="dp_no")]]
            await update.message.reply_text("No specific parser found. Proceed with generic conversion?", reply_markup=InlineKeyboardMarkup(keyboard))
            return CHAPTER_SELECTION
        await display_chapter_selection(update, context, f"Found {len(chapters)} chapters for '{title}'.")
        return CHAPTER_SELECTION
    except FileNotFoundError as e:
        logger.error(f"A required file was not found: {e}", exc_info=True)
        await update.message.reply_text(f"Error: A required file is missing. Please contact the developer. Details: {e}")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Failed to get chapters: {e}", exc_info=True)
        await update.message.reply_text(f"Could not fetch chapters. Error: {e}")
        return ConversationHandler.END

async def handle_default_parser_choice(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == 'dp_yes':
        await display_chapter_selection(update, context, "Proceeding with generic conversion...")
        return CHAPTER_SELECTION
    else:
        await query.edit_message_text("Operation cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

# --- Main Application Setup ---
def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    # Add all command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("update_parsers", update_parsers_command))
    application.add_handler(CommandHandler("load", load_parsers_command))
    application.add_handler(CommandHandler("cleanparsers", clean_parsers_command))
    # Conversation handlers
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('epub', epub_command)],
        states={CHAPTER_SELECTION: [CallbackQueryHandler(chapter_selection_callback, pattern='^(toggle|page|select|done)'), CallbackQueryHandler(handle_default_parser_choice, pattern='^dp_')]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('add_parser', add_parser_start)],
        states={TARGET_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_target_url)], PARSER_FILE: [MessageHandler(filters.Document.FileExtension("js"), received_parser_file)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_settings_callback, pattern='^set_')],
        states={SETTING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_setting_value_input)]},
        fallbacks=[CommandHandler('cancel', cancel)],
        map_to_parent={ConversationHandler.END: ConversationHandler.END}
    ))
    application.add_handler(CallbackQueryHandler(handle_settings_callback, pattern='^(toggle_|goto_|back_to_)'))
    # Run the bot
    application.run_webhook(listen="0.0.0.0", port=PORT, url_path=TELEGRAM_BOT_TOKEN, webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}")

if __name__ == '__main__':
    main()
