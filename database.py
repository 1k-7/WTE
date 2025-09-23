import pymongo
import os
from urllib.parse import urlparse

# --- Database Connection ---
MONGODB_URI = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/')
client = pymongo.MongoClient(MONGODB_URI)
db = client['webtoepub_bot']
user_settings_collection = db['user_settings']
repo_parsers_collection = db['repo_parsers']
custom_parsers_collection = db['custom_parsers']

# Create indexes for faster lookups
repo_parsers_collection.create_index([("domain", pymongo.ASCENDING)])
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
    """Saves a list of parsers from the repository to the database."""
    if not parsers_data:
        return
    repo_parsers_collection.delete_many({})  # Clear existing parsers
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
    """Finds a repository parser that matches the domain of the URL."""
    try:
        domain = urlparse(url).netloc.replace("www.","")
        # Find exact match first, then broader matches
        for i in range(domain.count('.') + 1):
            subdomain = '.'.join(domain.split('.')[i:])
            parser = repo_parsers_collection.find_one({'domain': subdomain})
            if parser:
                return parser
    except Exception:
        return None
    return None
