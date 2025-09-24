import pymongo
import os
from urllib.parse import urlparse
import logging
import re

logger = logging.getLogger(__name__)

# --- Database Connection ---
MONGODB_URI = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/')
client = pymongo.MongoClient(MONGODB_URI)
db = client['webtoepub_bot']
user_settings_collection = db['user_settings']
repo_parsers_collection = db['repo_parsers']
custom_parsers_collection = db['custom_parsers']

repo_parsers_collection.create_index([("domains", pymongo.ASCENDING)])
custom_parsers_collection.create_index([("user_id", pymongo.ASCENDING), ("target_url", pymongo.ASCENDING)])


# --- Settings Functions ---
def get_user_settings(user_id):
    """Retrieves settings for a given user from MongoDB."""
    return user_settings_collection.find_one({'user_id': user_id})

def set_user_setting(user_id, key, value):
    """Updates a specific setting for a user in MongoDB."""
    user_settings_collection.update_one(
        {'user_id': user_id},
        {'$set': {key: value}},
        upsert=True
    )

# --- Parser Functions ---
def save_parsers_from_repo(parsers_data):
    """
    Saves a list of parsers to the database incrementally.
    """
    if not parsers_data:
        return 0
    
    from pymongo import UpdateOne
    operations = [
        UpdateOne({'filename': p['filename']}, {'$set': p}, upsert=True)
        for p in parsers_data
    ]
    result = repo_parsers_collection.bulk_write(operations)
    return result.upserted_count + result.modified_count
    
def get_parser_count():
    """Returns the number of parsers loaded from the repository."""
    return repo_parsers_collection.count_documents({})

def clean_repo_parsers():
    """Deletes all parsers from the repository collection."""
    result = repo_parsers_collection.delete_many({})
    return result.deleted_count

def add_custom_parser(user_id, target_url, script_content):
    """Adds or updates a custom parser for a user."""
    custom_parsers_collection.update_one(
        {'user_id': user_id, 'target_url': target_url},
        {'$set': {'script': script_content}},
        upsert=True
    )

def get_custom_parser(user_id, url):
    """Finds a custom parser for a given URL and user."""
    return custom_parsers_collection.find_one({'user_id': user_id, 'target_url': url})

def get_repo_parser(url):
    """
    Finds the correct parser by matching the URL's domain against the
    'domains' array stored in the database.
    This function now uses a more robust regex-based matching to handle subdomains.
    """
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            logger.warning(f"Could not parse hostname from URL: {url}")
            return None
        
        hostname = hostname.lower()
        logger.info(f"Searching for parser for hostname: {hostname}")

        # Efficiently query for all potential matches using a regex that covers subdomains
        # This regex will match domains like 'www.example.com', 'm.example.com' if 'example.com' is in the database.
        # It will also match 'example.com' if 'www.example.com' is given.
        
        # To make it more robust, we will create a regex that matches any subdomain of the given hostname.
        # For example, if the hostname is "www.wuxiaworld.com", we create a regex that matches
        # "wuxiaworld.com", "www.wuxiaworld.com", "anything.wuxiaworld.com", etc.
        
        # We also need to handle cases where the database stores "wuxiaworld.com" and the user provides
        # "www.wuxiaworld.com" or "m.wuxiaworld.com"
        
        # We'll try a few strategies, from most specific to most general.
        
        # 1. Exact match
        parser = repo_parsers_collection.find_one({"domains": hostname})
        if parser:
            logger.info(f"Found exact match for {hostname}: {parser['filename']}")
            return parser
            
        # 2. Match without "www." if it exists
        if hostname.startswith("www."):
            parser = repo_parsers_collection.find_one({"domains": hostname[4:]})
            if parser:
                logger.info(f"Found match for {hostname[4:]}: {parser['filename']}")
                return parser

        # 3. Match with "www." if it does not exist
        else:
            parser = repo_parsers_collection.find_one({"domains": f"www.{hostname}"})
            if parser:
                logger.info(f"Found match for www.{hostname}: {parser['filename']}")
                return parser

        # 4. Check for parent domain matches.
        # For example, if the hostname is "m.wuxiaworld.com", this will check for "wuxiaworld.com"
        parts = hostname.split('.')
        if len(parts) > 2:
            for i in range(1, len(parts) - 1):
                parent_domain = '.'.join(parts[i:])
                parser = repo_parsers_collection.find_one({"domains": parent_domain})
                if parser:
                    logger.info(f"Found parent domain match for {parent_domain}: {parser['filename']}")
                    return parser
        
        # 5. Final attempt with a more general regex
        # This is a bit of a catch-all, but it might help in some cases.
        # This will match any parser where the domain is a substring of the hostname.
        escaped_hostname = re.escape(hostname)
        parser = repo_parsers_collection.find_one({"domains": {"$regex": f".*{escaped_hostname}.*"}})
        if parser:
            logger.info(f"Found regex substring match for {hostname}: {parser['filename']}")
            return parser

        logger.warning(f"No parser found for hostname: {hostname}")

    except Exception as e:
        logger.error(f"Error finding repo parser for {url}: {e}", exc_info=True)
    
    return None
