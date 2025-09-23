import logging
import os
import json
import asyncio
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackContext, CallbackQueryHandler, ConversationHandler
)

from settings import (
    get_user_settings, handle_settings_callback,
    SETTING_VALUE, handle_setting_value_input, get_main_settings_menu
)
from parser import create_epub_from_url, update_parsers_from_github
from database import add_custom_parser

# --- Enable logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment variables & Constants ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')  # The public URL of your Render web service
PORT = int(os.environ.get('PORT', 8080)) # Render provides this automatically

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")
if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL environment variable is not set. This is required for webhook mode.")

# --- Conversation states ---
TARGET_URL, PARSER_FILE = range(2)


# --- Command Handlers ---

async def start(update: Update, context: CallbackContext) -> None:
    """Send a welcome message."""
    await update.message.reply_text(
        'Welcome to the WebToEpub Bot!\n\n'
        'Send me a link to convert it into an EPUB.\n'
        'Use /settings to configure options.\n'
        'Use /update_parsers to fetch the latest parsers.\n'
        'Use /add_parser to add a custom parser.\n'
        'Use /epub to process a list of links from a message or file.'
    )

async def settings_command(update: Update, context: CallbackContext) -> None:
    """Display the main settings menu."""
    user_id = update.message.from_user.id
    reply_markup, message = await get_main_settings_menu(user_id)
    await update.message.reply_text(message, reply_markup=reply_markup)

async def update_parsers_command(update: Update, context: CallbackContext) -> None:
    """Update parsers from the GitHub repository."""
    await update.message.reply_text("Updating parsers... This may take a moment.")
    try:
        count = await update_parsers_from_github()
        await update.message.reply_text(f"Successfully updated {count} parsers.")
    except Exception as e:
        logger.error(f"Error updating parsers: {e}")
        await update.message.reply_text("An error occurred while updating parsers.")

async def add_parser_start(update: Update, context: CallbackContext) -> int:
    """Start the custom parser upload process."""
    await update.message.reply_text("Please provide the target URL for this parser (e.g., https://www.example.com).")
    return TARGET_URL

async def received_target_url(update: Update, context: CallbackContext) -> int:
    """Receive target URL and ask for the parser file."""
    context.user_data['target_url'] = update.message.text
    await update.message.reply_text("Thank you. Now, please upload the .js parser file.")
    return PARSER_FILE

async def received_parser_file(update: Update, context: CallbackContext) -> int:
    """Receive parser file and save it."""
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
    """Cancel any ongoing conversation."""
    if 'setting_to_edit' in context.user_data:
        del context.user_data['setting_to_edit']
    await update.message.reply_text('Operation cancelled.')
    return ConversationHandler.END

# --- Link Processing ---

async def process_single_link(update: Update, context: CallbackContext, url: str) -> None:
    """Processes a single URL and sends the EPUB."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    processing_message = await context.bot.send_message(chat_id, f"Processing: {url}")
    
    try:
        user_settings = get_user_settings(user_id)
        epub_path, title = await create_epub_from_url(url, user_settings, user_id)

        if epub_path and os.path.exists(epub_path):
            with open(epub_path, 'rb') as epub_file:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=epub_file,
                    filename=f"{title}.epub",
                    caption=f"Successfully created EPUB for: {url}"
                )
            os.remove(epub_path)
        else:
            await context.bot.send_message(chat_id, f"Failed to create EPUB for: {url}\nThe site may not be supported or the content is protected.")
    except Exception as e:
        logger.error(f"Error processing link {url}: {e}", exc_info=True)
        await context.bot.send_message(chat_id, f"An error occurred while processing: {url}")
    finally:
        await processing_message.delete()

async def process_url_list(update: Update, context: CallbackContext, urls: list) -> None:
    """Iterates through a list of URLs and processes them one by one."""
    total_urls = len(urls)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Starting batch process for {total_urls} URL(s). This may take some time."
    )

    for i, url in enumerate(urls):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Processing {i+1}/{total_urls}: {url}"
        )
        await process_single_link(update, context, url)
        await asyncio.sleep(2)  # Small delay between processing
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Batch processing complete."
    )

async def handle_link(update: Update, context: CallbackContext) -> None:
    """Handle incoming messages containing links for EPUB conversion."""
    urls = filters.Entity("url").extract_from(update.message)
    if not urls:
        await update.message.reply_text("Please send me a valid URL or a list of URLs.")
        return
    await process_url_list(update, context, list(urls))
    
async def epub_command(update: Update, context: CallbackContext) -> None:
    """Handles /epub command for batch processing from text or file."""
    replied_message = update.message.reply_to_message
    urls = []

    if replied_message and replied_message.document:
        doc = replied_message.document
        if doc.file_name.endswith(('.txt', '.json')):
            try:
                file = await context.bot.get_file(doc.file_id)
                file_content_bytes = await file.download_as_bytearray()
                file_content = file_content_bytes.decode('utf-8')

                if doc.file_name.endswith('.txt'):
                    urls = [line.strip() for line in file_content.splitlines() if line.strip()]
                elif doc.file_name.endswith('.json'):
                    urls = json.loads(file_content)
                    if not isinstance(urls, list): raise ValueError("JSON must be a list of URLs.")
            except Exception as e:
                logger.error(f"Error processing file for /epub: {e}")
                await update.message.reply_text(f"Could not process the file: {e}")
                return
        else:
            await update.message.reply_text("Please reply to a .txt or .json file.")
            return
    else:
        # Check current message for URLs if not a reply
        target_message = replied_message if replied_message else update.message
        urls = filters.Entity("url").extract_from(target_message)
        if not urls:
             await update.message.reply_text("Reply to a message with links or a link file with /epub.")
             return

    if urls:
        await process_url_list(update, context, list(urls))
    else:
        await update.message.reply_text("No valid URLs found to process.")

# --- Callback and Conversation Handlers ---

async def settings_callback_handler(update: Update, context: CallbackContext) -> None:
    """Handle all button presses in settings menus."""
    query = update.callback_query
    await query.answer()
    return await handle_settings_callback(query, context)

def main() -> None:
    """Start the bot using webhooks for deployment as a web service."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    add_parser_handler = ConversationHandler(
        entry_points=[CommandHandler('add_parser', add_parser_start)],
        states={
            TARGET_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_target_url)],
            PARSER_FILE: [MessageHandler(filters.Document.JS, received_parser_file)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    set_setting_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(settings_callback_handler, pattern='^set_')],
        states={SETTING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_setting_value_input)]},
        fallbacks=[CommandHandler('cancel', cancel)],
        map_to_parent={ConversationHandler.END: ConversationHandler.END}
    )
    
    # Add all handlers
    application.add_handler(add_parser_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("update_parsers", update_parsers_command))
    application.add_handler(CommandHandler("epub", epub_command))
    application.add_handler(MessageHandler(filters.Entity("url") & ~filters.COMMAND, handle_link))
    application.add_handler(CallbackQueryHandler(settings_callback_handler, pattern='^(toggle_|goto_|back_to_)'))
    application.add_handler(set_setting_handler)

    # Start the bot via webhook
    # The URL path is the token, which is a secret way to ensure only Telegram is calling this endpoint.
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}"
    )

if __name__ == '__main__':
    main()
