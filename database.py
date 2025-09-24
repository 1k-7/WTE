import pymongo
import os
import re
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

        # 1. Exact match first for performance
        logger.info(f"Attempting exact match for: {hostname}")
        parser = repo_parsers_collection.find_one({"domains": hostname})
        if parser:
            logger.info(f"Found exact match: {parser['filename']}")
            return parser

        # 2. Regex match for subdomains and parent domains
        # This will match domains like 'www.example.com', 'm.example.com' if 'example.com' is in the database.
        # It will also match 'example.com' if 'www.example.com' is given.
        parts = hostname.split('.')
        domain_variants = [hostname]
        if hostname.startswith("www."):
            domain_variants.append(hostname[4:])
        else:
            domain_variants.append(f"www.{hostname}")
        
        if len(parts) > 2:
            for i in range(1, len(parts) - 1):
                parent_domain = '.'.join(parts[i:])
                domain_variants.append(parent_domain)
                if not parent_domain.startswith("www."):
                    domain_variants.append(f"www.{parent_domain}")

        # Create a list of regex patterns to try
        regex_patterns = []
        for variant in set(domain_variants):
            # regex to match the domain, and any subdomains
            regex_patterns.append(f"^(www\\.)?{re.escape(variant.replace('www.',''))}$")

        logger.info(f"Attempting regex match with patterns: {regex_patterns}")

        for pattern in regex_patterns:
            # Using $regex operator in pymongo
            parser = repo_parsers_collection.find_one({"domains": {"$regex": pattern, "$options": "i"}})
            if parser:
                logger.info(f"Found regex match with pattern '{pattern}': {parser['filename']}")
                return parser
        
        logger.warning(f"No parser found for hostname: {hostname}")

    except Exception as e:
        logger.error(f"Error finding repo parser for {url}: {e}", exc_info=True)
    
    return None
