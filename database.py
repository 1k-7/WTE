import pymongo
import os
from urllib.parse import urlparse
import logging

logger = logging.getLogger(__name__)

# --- Database Connection ---
MONGODB_URI = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/')
client = pymongo.MongoClient(MONGODB_URI)
db = client['webtoepub_bot']
user_settings_collection = db['user_settings']
repo_parsers_collection = db['repo_parsers']
custom_parsers_collection = db['custom_parsers']

# --- New, more effective index ---
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
    """Saves a list of parsers and their registered domains to the database."""
    if not parsers_data:
        return
    repo_parsers_collection.delete_many({})
    repo_parsers_collection.insert_many(parsers_data)
    
def get_parser_count():
    """Returns the number of parsers loaded from the repository."""
    return repo_parsers_collection.count_documents({})

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
    'domains' array stored in the database. This mimics the extension's logic.
    """
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return None

        # 1. Try a direct, exact match first (e.g., 'www.royalroad.com')
        parser = repo_parsers_collection.find_one({"domains": hostname})
        if parser:
            logger.info(f"Found direct parser match for {hostname}: {parser['filename']}")
            return parser

        # 2. If no exact match, try matching parent domains.
        #    (e.g., if hostname is 'sub.domain.com', it will check 'domain.com')
        parts = hostname.split('.')
        if len(parts) > 1:
            for i in range(1, len(parts)):
                parent_domain = '.'.join(parts[i:])
                parser = repo_parsers_collection.find_one({"domains": parent_domain})
                if parser:
                    logger.info(f"Found parent domain parser match for {parent_domain}: {parser['filename']}")
                    return parser
    except Exception as e:
        logger.error(f"Error finding repo parser for {url}: {e}")
    
    return None
