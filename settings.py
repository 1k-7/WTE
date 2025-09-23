from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler
from database import get_user_settings as db_get_settings, set_user_setting as db_set_setting, get_parser_count

# --- Setting Definitions ---

SETTING_METADATA = {
    'content': {
        'label': 'Content & Formatting',
        'settings': {
            'removeAuthorNotes': {'type': 'bool', 'label': 'Remove Author Notes'},
            'removeChapterNumber': {'type': 'bool', 'label': 'Remove Chapter Numbers'},
            'removeOriginal': {'type': 'bool', 'label': 'Remove Original/Raw Text'},
            'removeTranslated': {'type': 'bool', 'label': 'Remove Translated Text'},
            'unSuperScriptAlternateTranslations': {'type': 'bool', 'label': 'Remove Superscript Translations'},
            'removeNextAndPreviousChapterHyperlinks': {'type': 'bool', 'label': 'Remove Next/Prev Chapter Links'},
            'addInformationPage': {'type': 'bool', 'label': 'Add Information Page'},
        }
    },
    'images': {
        'label': 'Image Settings',
        'settings': {
            'skipImages': {'type': 'bool', 'label': 'Skip All Images'},
            'removeDuplicateImages': {'type': 'bool', 'label': 'Remove Duplicate Images'},
            'includeImageSourceUrl': {'type': 'bool', 'label': 'Include Image Source URL'},
            'highestResolutionImages': {'type': 'bool', 'label': 'Fetch Highest Resolution Images'},
            'useSvgForImages': {'type': 'bool', 'label': 'Use SVG for Images'},
            'compressImages': {'type': 'bool', 'label': 'Compress Images'},
            'compressImagesJpgCover': {'type': 'bool', 'label': 'Restrict Cover to JPEG'},
            'compressImagesType': {'type': 'choice', 'label': 'Compressed Image Format', 'options': ['auto', 'jpg', 'webp', 'png']},
            'compressImagesMaxResolution': {'type': 'int', 'label': 'Compressed Resolution'},
        }
    },
    'epub': {
        'label': 'EPUB Structure',
        'settings': {
            'createEpub3': {'type': 'bool', 'label': 'Create EPUB 3'},
            'chaptersPageInChapterList': {'type': 'bool', 'label': 'Add Chapters Page to List'},
            'maxChaptersPerEpub': {'type': 'int', 'label': 'Max Chapters per EPUB'},
            'useFullTitleAsFileName': {'type': 'bool', 'label': 'Use Full Title as Filename'},
            'CustomFilename': {'type': 'str', 'label': 'Custom Filename Template'},
        }
    },
    'advanced': {
        'label': 'Advanced & Network',
        'settings': {
            'load_parsers_from_repo': {'type': 'bool', 'label': 'Auto-load Parsers from Repo'},
            'skipChaptersThatFailFetch': {'type': 'bool', 'label': 'Skip Chapters That Fail'},
            'manualDelayPerChapter': {'type': 'int', 'label': 'Delay Per Chapter (ms)'},
            'overrideMinimumDelay': {'type': 'bool', 'label': 'Override Parser Min Delay'},
        }
    }
}

DEFAULT_SETTINGS = {key: details['settings'][key].get('default', False) if details['settings'][key]['type'] == 'bool' else
                    (10000 if key == 'maxChaptersPerEpub' else
                     1080 if key == 'compressImagesMaxResolution' else
                     0 if key == 'manualDelayPerChapter' else
                     'jpg' if key == 'compressImagesType' else
                     '%Filename%' if key == 'CustomFilename' else
                     True if key in ['includeImageSourceUrl', 'highestResolutionImages', 'useSvgForImages', 'removeNextAndPreviousChapterHyperlinks', 'addInformationPage', 'load_parsers_from_repo', 'removeOriginal'] else
                     False)
                    for _, details in SETTING_METADATA.items() for key in details['settings']}
DEFAULT_SETTINGS['createEpub3'] = False # Special case

# Conversation state
SETTING_VALUE = 0

# --- Settings Logic ---

def get_user_settings(user_id):
    """Retrieves settings for a given user from the database."""
    settings = db_get_settings(user_id)
    full_settings = DEFAULT_SETTINGS.copy()
    if settings:
        full_settings.update(settings)
    return full_settings

def set_user_setting(user_id, key, value):
    """Updates a single setting for a user."""
    # Type conversion for numeric settings
    setting_type = next((details['settings'][key]['type'] for _, details in SETTING_METADATA.items() if key in details['settings']), None)
    if setting_type == 'int':
        try:
            value = int(value)
        except (ValueError, TypeError):
            # Handle error or set a default if conversion fails
            print(f"Could not convert {value} to int for setting {key}")
            return
    db_set_setting(user_id, key, value)


async def get_main_settings_menu(user_id):
    """Generates the main settings menu keyboard."""
    keyboard = []
    for category_key, category_details in SETTING_METADATA.items():
        keyboard.append([InlineKeyboardButton(category_details['label'], callback_data=f'goto_{category_key}')])
    
    parser_count = get_parser_count()
    message = f"WebToEpub Settings:\n\nSelect a category to view or edit settings.\n\nLoaded Parsers from Repo: {parser_count}"
    return InlineKeyboardMarkup(keyboard), message

async def get_category_settings_menu(user_id, category_key):
    """Generates the settings menu for a specific category."""
    settings = get_user_settings(user_id)
    category = SETTING_METADATA[category_key]
    
    keyboard = []
    for key, details in category['settings'].items():
        current_value = settings.get(key)
        
        if details['type'] == 'bool':
            status_emoji = "✅" if current_value else "❌"
            button_text = f"{status_emoji} {details['label']}"
            callback_data = f"toggle_{key}"
        else:
            button_text = f"{details['label']}: {current_value}"
            callback_data = f"set_{key}"
            
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
    keyboard.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main")])
    message = f"Settings for: {category['label']}"
    return InlineKeyboardMarkup(keyboard), message

async def handle_settings_callback(query, context):
    """Main router for all settings-related callback queries."""
    user_id = query.from_user.id
    callback_data = query.data
    
    action, _, key = callback_data.partition('_')

    if action == 'toggle':
        current_settings = get_user_settings(user_id)
        new_value = not current_settings.get(key, False)
        set_user_setting(user_id, key, new_value)
        if key == 'load_parsers_from_repo' and new_value:
             await query.edit_message_text(text="Updating parsers from repository...")
             from parser import update_parsers_from_github
             await update_parsers_from_github()
        
        category_key = next((cat for cat, details in SETTING_METADATA.items() if key in details['settings']), None)
        reply_markup, message = await get_category_settings_menu(user_id, category_key)
        await query.edit_message_text(text=message, reply_markup=reply_markup)
        
    elif action == 'goto':
        reply_markup, message = await get_category_settings_menu(user_id, key)
        await query.edit_message_text(text=message, reply_markup=reply_markup)
        
    elif action == 'back':
        reply_markup, message = await get_main_settings_menu(user_id)
        await query.edit_message_text(text=message, reply_markup=reply_markup)

    elif action == 'set':
        context.user_data['setting_to_edit'] = key
        await query.message.reply_text(f"Please enter the new value for '{key.replace('_', ' ').title()}'.\nType /cancel to abort.")
        return SETTING_VALUE

async def handle_setting_value_input(update: Update, context: CallbackContext) -> int:
    """Handles the user's input for a setting value."""
    user_id = update.message.from_user.id
    setting_key = context.user_data.get('setting_to_edit')
    new_value = update.message.text
    
    if setting_key:
        set_user_setting(user_id, setting_key, new_value)
        await update.message.reply_text(f"Setting '{setting_key.replace('_', ' ').title()}' updated to '{new_value}'.")
        del context.user_data['setting_to_edit']

        # Show the settings menu again
        await context.bot.send_message(chat_id=user_id, text="Returning to settings...")
        reply_markup, message = await get_main_settings_menu(user_id)
        await context.bot.send_message(chat_id=user_id, text=message, reply_markup=reply_markup)

    return ConversationHandler.END
