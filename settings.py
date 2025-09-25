import logging
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, ConversationHandler
from database import get_user_settings as db_get_settings, set_user_setting as db_set_setting, get_parser_count

logger = logging.getLogger(__name__)

# --- Conversation states ---
SETTING_VALUE = 0

# --- Settings Definition ---
SETTINGS = {
    'remove_hyperlinks': {'text': 'Remove Hyperlinks', 'type': 'toggle', 'default': True},
    'remove_images': {'text': 'Remove Images', 'type': 'toggle', 'default': False},
}
DEFAULT_SETTINGS = {key: props['default'] for key, props in SETTINGS.items()}

def get_user_settings(user_id: int) -> dict:
    """Gets user settings from DB, providing defaults for missing values."""
    settings = db_get_settings(user_id)
    if not settings:
        return DEFAULT_SETTINGS.copy()
    
    # Ensure all keys have a value, falling back to default if not present
    for key, default_value in DEFAULT_SETTINGS.items():
        if key not in settings:
            settings[key] = default_value
            
    return settings

async def get_main_settings_menu(user_id: int):
    """Creates the main settings menu keyboard and text."""
    user_settings_sync = await asyncio.to_thread(get_user_settings, user_id)
    
    keyboard = []
    for key, props in SETTINGS.items():
        if props['type'] == 'toggle':
            status = "✅" if user_settings_sync.get(key) else "❌"
            keyboard.append([InlineKeyboardButton(f"{props['text']}: {status}", callback_data=f"toggle_{key}")])
    
    parser_count = await asyncio.to_thread(get_parser_count)
    message = f"⚙️ **Bot Settings**\n\nConfigure the bot's behavior. Currently tracking `{parser_count}` repository parsers."
    
    return InlineKeyboardMarkup(keyboard), message

async def handle_settings_callback(update: Update, context: CallbackContext):
    """Handles button presses in the settings menu."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    action, _, setting_key = query.data.partition('_')

    if action == 'toggle':
        current_settings = await asyncio.to_thread(db_get_settings, user_id)
        current_value = current_settings.get(setting_key, DEFAULT_SETTINGS.get(setting_key))
        new_value = not current_value
        await asyncio.to_thread(db_set_setting, user_id, setting_key, new_value)
    
    # Refresh the menu
    reply_markup, message = await get_main_settings_menu(user_id)
    await query.edit_message_text(text=message, reply_markup=reply_markup)


async def handle_setting_value_input(update: Update, context: CallbackContext) -> int:
    """Handles text input for a setting value."""
    user_id = update.message.from_user.id
    setting = context.user_data.get('setting_to_set')
    new_value = update.message.text

    if not setting:
        await update.message.reply_text("An error occurred. Please try again.")
        return ConversationHandler.END

    await asyncio.to_thread(db_set_setting, user_id, setting, new_value)
    await update.message.reply_text(f"Setting `{setting}` updated successfully!")
    
    # Show the main menu again
    reply_markup, message = await get_main_settings_menu(user_id)
    await update.message.reply_text(message, reply_markup=reply_markup)
    
    context.user_data.clear()
    return ConversationHandler.END
