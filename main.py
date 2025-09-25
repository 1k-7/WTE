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
from parser import get_chapter_list, create_epub_from_chapters, generate_parsers_manifest, ensure_parsers_are_loaded
from database import add_custom_parser, clean_database, set_log_channel, get_log_channel

# --- Enable logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment variables & Constants ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
PORT = int(os.environ.get('PORT', 8080))

# --- Conversation states ---
TARGET_URL, PARSER_FILE = range(2)
CHAPTER_SELECTION = 0

# --- Helper Functions ---
async def log_to_channel(context: CallbackContext, message: str):
    """Sends a log message to the configured log channel."""
    log_channel_id = get_log_channel()
    if log_channel_id:
        try:
            await context.bot.send_message(chat_id=log_channel_id, text=message)
        except Exception as e:
            logger.error(f"Failed to send log to channel {log_channel_id}: {e}")

# --- Command Handlers ---

async def start(update: Update, context: CallbackContext) -> None:
    """Send a welcome message."""
    start_message = (
        'Welcome to the WebToEpub Bot!\n\n'
        '**Instructions:**\n'
        '1. If you have a logs channel, make the bot an admin and use `/logc` to set it.\n'
        '2. Use `/cleandb` to ensure a fresh start.\n'
        '3. The bot will automatically load parsers from `parsers.json` on the first `/epub` command.\n\n'
        '**Available Commands:**\n'
        '/epub <url> - Convert a page.\n'
        '/settings - Configure options.\n'
        '/add_parser - Add a custom parser.\n'
        '/cleandb - (Admin) Wipes the entire database.\n'
        '/logc - (Admin) Sets the channel for detailed logs.\n'
        '/parserjson - (Admin) Generate the `parsers.json` file from your local parsers.'
    )
    await update.message.reply_text(start_message)
    
async def clean_db_command(update: Update, context: CallbackContext) -> None:
    """Clears the entire database."""
    await update.message.reply_text("Cleaning all collections from the database...")
    try:
        deleted_counts = clean_database()
        response_message = "Successfully cleaned the database.\n"
        for collection_name, count in deleted_counts.items():
            response_message += f"- Deleted {count} documents from `{collection_name}`\n"
        await update.message.reply_text(response_message)
        await log_to_channel(context, f"Database cleaned by user {update.effective_user.id}.")
    except Exception as e:
        logger.error(f"Error cleaning database: {e}", exc_info=True)
        await update.message.reply_text("An error occurred while cleaning the database.")

async def set_log_channel_command(update: Update, context: CallbackContext) -> None:
    """Sets the log channel ID."""
    if not context.args:
        await update.message.reply_text("Usage: /logc <channel_id>")
        return
    
    try:
        channel_id = context.args[0]
        set_log_channel(channel_id)
        await update.message.reply_text(f"Log channel has been set to: {channel_id}")
        await log_to_channel(context, f"Log channel was set by user {update.effective_user.id}.")
    except Exception as e:
        logger.error(f"Error setting log channel: {e}", exc_info=True)
        await update.message.reply_text("An error occurred while setting the log channel.")

async def settings_command(update: Update, context: CallbackContext) -> None:
    """Display the main settings menu."""
    user_id = update.message.from_user.id
    reply_markup, message = await get_main_settings_menu(user_id)
    await update.message.reply_text(message, reply_markup=reply_markup)

async def generate_manifest_command(update: Update, context: CallbackContext) -> None:
    """Triggers the generation of the parsers.json manifest file."""
    sent_message = await update.message.reply_text("Starting `parsers.json` generation... This will take a very long time and the bot will be unresponsive.")
    await log_to_channel(context, "Parser manifest generation started.")
    asyncio.create_task(generate_parsers_manifest(sent_message))


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

# --- Chapter Selection and EPUB Creation ---
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
    message = f"Creating EPUB for '{title}'..."
    await context.bot.send_message(chat_id, message)
    await log_to_channel(context, message)
    
    try:
        user_settings = get_user_settings(update.effective_user.id)
        epub_path, final_filename = await create_epub_from_chapters(chapters, title, user_settings)
        if epub_path and os.path.exists(epub_path):
            with open(epub_path, 'rb') as epub_file:
                await context.bot.send_document(chat_id=chat_id, document=epub_file, filename=f"{final_filename}.epub", caption=f"EPUB for: {title}")
            os.remove(epub_path)
            await log_to_channel(context, f"Successfully created and sent EPUB for '{title}'.")
        else:
            await context.bot.send_message(chat_id, f"Failed to create EPUB for: {title}")
            await log_to_channel(context, f"Failed to create EPUB for '{title}'.")
    except Exception as e:
        logger.error(f"Error creating EPUB: {e}", exc_info=True)
        await context.bot.send_message(chat_id, f"An error occurred: {e}")
        await log_to_channel(context, f"Error creating EPUB for '{title}': {e}")

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
    await log_to_channel(context, f"Received /epub command for: {url}")
    
    try:
        # This function now ensures parsers are loaded if they haven't been already.
        await ensure_parsers_are_loaded(context)
        
        title, chapters, parser_found = await get_chapter_list(url, update.effective_user.id, context)
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
    except Exception as e:
        logger.error(f"Failed to get chapters: {e}", exc_info=True)
        await update.message.reply_text(f"Could not fetch chapters. Error: {e}")
        await log_to_channel(context, f"Failed to get chapters for {url}. Error: {e}")
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

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("parserjson", generate_manifest_command))
    application.add_handler(CommandHandler("cleandb", clean_db_command))
    application.add_handler(CommandHandler("logc", set_log_channel_command))
    
    # Conversation handlers
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('loadparsers', load_parsers_start)],
        states={LOAD_PARSER_FILE: [MessageHandler(filters.Document.FileExtension("json"), received_parsers_file)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    ))
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
