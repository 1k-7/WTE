from pymongo import MongoClient
from urllib.parse import urlparse
import logging
import os

# --- Constants ---
MONGO_URI = os.environ.get('MONGO_URI')
DB_NAME = "wte-bot-db"

# --- Database Client ---
client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# --- Collections ---
user_settings = db["user_settings"]
custom_parsers = db["custom_parsers"]
repo_parsers = db["repo_parsers"] 
log_channel = db["log_channel"]

# --- Logging ---
logger = logging.getLogger(__name__)

# --- Settings Management (FIXED) ---

def get_user_settings(user_id: int) -> dict:
    """
    Retrieves a user's settings document from the database.
    Returns an empty dict if no settings are found.
    """
    settings = user_settings.find_one({"user_id": user_id})
    return settings if settings else {}

def set_user_setting(user_id: int, key: str, value):
    """
    Sets a specific setting for a user in the database.
    Creates the user's settings document if it doesn't exist.
    """
    user_settings.update_one(
        {"user_id": user_id},
        {"$set": {key: value}},
        upsert=True
    )

# --- Parser Management ---

def get_parser_count():
    """Returns the number of parsers in the database."""
    return repo_parsers.count_documents({})

def get_repo_parser(url: str):
    """
    Finds a parser from the repository collection that matches the given URL's domain.
    Handles 'www.' subdomains for more reliable matching.
    """
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return None
            
        # Create a list of possible domains to check (e.g., 'www.example.com' and 'example.com')
        domains_to_check = [hostname]
        if hostname.startswith('www.'):
            domains_to_check.append(hostname[4:])
            
        # Query for a parser where its 'domains' array contains any of our possible domains.
        parser = repo_parsers.find_one({"domains": {"$in": domains_to_check}})
        
        if parser:
            logger.info(f"Found repo parser for {hostname}: {parser.get('filename')}")
        else:
            logger.info(f"No repo parser found for any of: {domains_to_check}")
            
        return parser
    except Exception as e:
        logger.error(f"Error fetching repo parser for {url}: {e}", exc_info=True)
        return None

def add_custom_parser(user_id: int, url: str, script_content: str):
    """Adds or updates a custom parser for a user."""
    hostname = urlparse(url).hostname
    if not hostname:
        raise ValueError("Invalid URL provided.")
    
    custom_parsers.update_one(
        {"user_id": user_id, "hostname": hostname},
        {"$set": {"script": script_content}},
        upsert=True
    )
    logger.info(f"Upserted custom parser for user {user_id} and host {hostname}")

def get_custom_parser(user_id: int, url: str):
    """Retrieves a user's custom parser for a given URL."""
    hostname = urlparse(url).hostname
    if not hostname:
        return None
    return custom_parsers.find_one({"user_id": user_id, "hostname": hostname})

def save_parsers_from_repo(parsers_list: list):
    """Saves a list of parsers to the repo_parsers collection."""
    if not parsers_list:
        return 0
    try:
        # Using insert_many for bulk operation
        result = repo_parsers.insert_many(parsers_list, ordered=False)
        return len(result.inserted_ids)
    except Exception as e:
        logger.error(f"Error during bulk save of repo parsers: {e}", exc_info=True)
        return 0

def clean_all_parsers():
    """Removes all documents from the repo_parsers collection."""
    try:
        result = repo_parsers.delete_many({})
        logger.info(f"Cleaned {result.deleted_count} parsers from the repository collection.")
        return result.deleted_count
    except Exception as e:
        logger.error(f"Error cleaning repo parsers: {e}", exc_info=True)
        return 0

# --- General Database Functions ---

def clean_database():
    """Wipes all collections in the database."""
    deleted_counts = {}
    for collection in [user_settings, custom_parsers, repo_parsers, log_channel]:
        count = collection.delete_many({}).deleted_count
        deleted_counts[collection.name] = count
    return deleted_counts

def set_log_channel(channel_id: str):
    """Sets or updates the log channel ID."""
    log_channel.update_one(
        {"_id": "log_channel_config"},
        {"$set": {"channel_id": channel_id}},
        upsert=True
    )

def get_log_channel():
    """Retrieves the configured log channel ID."""
    config = log_channel.find_one({"_id": "log_channel_config"})
    return config.get("channel_id") if config else None
