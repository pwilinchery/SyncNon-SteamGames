import sys, io, os
import vdf
import requests
import logging
import zlib   
import json
import re
import random
import string
from pathlib import Path
from gooey import Gooey, GooeyParser

# Try to import psutil for process checking
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logging.warning("psutil not installed. Cannot check if Steam is running.")
    logging.warning("Install with: pip install psutil --break-system-packages")

# Optional: force UTF-8 mode globally
os.environ["PYTHONIOENCODING"] = "utf-8"

# Rebind stdout/stderr to UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

def saveJsonFile(filename, data):
    with open(filename, 'w') as f:
        json.dump(data, f)

def readJsonFile(filename):
    if(os.path.isfile(filename)):
        with open(filename, 'r') as f:
            return json.loads(f.read())
        
def check_steam_running():
    """Check if Steam is currently running and warn the user"""
    if not PSUTIL_AVAILABLE:
        logger.warning("=" * 70)
        logger.warning("IMPORTANT: Make sure Steam is CLOSED before running this script!")
        logger.warning("=" * 70)
        logger.warning("")
        logger.warning("Steam overwrites localconfig.vdf when it exits, which will")
        logger.warning("erase any changes this script makes.")
        logger.warning("")
        logger.warning("Install psutil for automatic checking:")
        logger.warning("  pip install psutil --break-system-packages")
        logger.warning("")
        return False  # Can't check, continue anyway
    
    import psutil
    
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and 'steam.exe' in proc.info['name'].lower():
                logger.error("=" * 70)
                logger.error("STEAM IS CURRENTLY RUNNING!")
                logger.error("=" * 70)
                logger.error("")
                logger.error("Steam must be CLOSED before running this script.")
                logger.error("Steam overwrites localconfig.vdf when it exits, which will")
                logger.error("erase any changes this script makes.")
                logger.error("")
                logger.error("Please:")
                logger.error("  1. Close Steam completely")
                logger.error("  2. Wait a few seconds for it to fully exit")
                logger.error("  3. Run this script again")
                logger.error("")
                logger.error("=" * 70)
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    
    logger.info("✓ Steam is not running - safe to proceed")
    return False


def determineUserdataFolder():
    #Assuming only 1 steam user, on \\userdata we only have the '0' folder and the one with the ID, so we can infer it
    steam_user_data_ID = [x for x in os.listdir(os.path.join(steamdir_path,"userdata")) if x != "0"][0]
    return os.path.join(steamdir_path, "userdata", steam_user_data_ID, "config")


# ============================================================================
# LOCALCONFIG.VDF COLLECTION FUNCTIONS
# ============================================================================

def generate_collection_id():
    """Generate a unique collection ID in Steam's format (uc-XXXXXXXXXXXX)"""
    chars = string.ascii_letters + string.digits + '+/'
    random_part = ''.join(random.choices(chars, k=12))
    return f"uc-{random_part}"


def extract_user_collections(localconfig_content):
    """Extract the user-collections JSON from localconfig.vdf"""
    # Use the same robust pattern as write_user_collections
    match = re.search(r'"user-collections"\s+("(?:[^"\\]|\\.)*")', localconfig_content)
    
    if not match:
        return {}
    
    # Extract the escaped JSON string (without the outer quotes)
    escaped_json = match.group(1)[1:-1]  # Remove surrounding quotes
    
    # Unescape the JSON - order matters! Replace escaped quotes first, then backslashes
    json_str = escaped_json.replace('\\"', '"').replace('\\\\', '\\')
    
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning(f"Could not parse existing user-collections: {e}")
        logger.debug(f"Problematic JSON string: {json_str[:200]}")
        return {}


def read_cloud_storage_collections(steam_user_data_path):
    """
    Read collection names from Steam's cloud storage JSON file.

    Steam stores collection metadata in userdata/<userid>/config/cloudstorage/cloud-storage-namespace-1.json
    This is the definitive source of collection names.

    Args:
        steam_user_data_path: Path to Steam's userdata config folder

    Returns:
        dict mapping collection_id -> name, or empty dict if unavailable
    """
    collection_names = {}

    # Try to read from cloud storage JSON
    cloud_storage_path = os.path.join(steam_user_data_path, 'config', 'cloudstorage', 'cloud-storage-namespace-1.json')

    if not os.path.exists(cloud_storage_path):
        logger.warning(f"Cloud storage file not found at {cloud_storage_path}")
        return {}
    
    collection_names = {}

    try:
        logger.info(f"Reading collections from cloud storage: {cloud_storage_path}")

        with open(cloud_storage_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Parse the cloud storage data
        for entry in data:
            if len(entry) == 2:
                key, value_data = entry

                # Look for user-collections entries
                if key.startswith('user-collections.') and not value_data.get('is_deleted'):
                    coll_id = key.replace('user-collections.', '')

                    # Parse the value JSON
                    if 'value' in value_data:
                        try:
                            coll_info = json.loads(value_data['value'])
                            name = coll_info.get('name', 'Unknown')

                            # Store collection name (coll_id -> name)
                            collection_names[coll_id] = name

                        except json.JSONDecodeError:
                            pass

        logger.info(f"Found {len(collection_names)} collections in cloud storage")
        for coll_id, name in list(collection_names.items())[:10]:  # Show first 10
            logger.info(f"  - {name} ({coll_id})")

    except Exception as e:
        logger.warning(f"Could not read from cloud storage: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return {}

    return collection_names



def update_collection_with_appids(collections, collection_id, appids):
    """Update a collection to include all given AppIDs"""
    if collection_id not in collections:
        collections[collection_id] = {
            "id": collection_id,
            "added": [],
            "removed": []
        }
    
    # Add all AppIDs to the 'added' list (avoid duplicates)
    current_added = set(collections[collection_id]["added"])
    current_added.update(appids)
    collections[collection_id]["added"] = sorted(list(current_added))
    
    # Clear removed list
    collections[collection_id]["removed"] = []


def write_user_collections(localconfig_content, collections):
    """Write the updated collections back to localconfig.vdf content"""
    # Convert collections to JSON
    json_str = json.dumps(collections, separators=(',', ':'))
    
    # Escape for VDF format - order matters! Backslashes first, then quotes
    escaped_json = json_str.replace('\\', '\\\\').replace('"', '\\"')
    
    # Find and replace the user-collections line
    # Use a more specific pattern to avoid matching partial strings
    pattern = r'"user-collections"\s+("(?:[^"\\]|\\.)*")'
    replacement = f'"user-collections"\t\t"{escaped_json}"'
    
    if re.search(pattern, localconfig_content):
        new_content = re.sub(pattern, replacement, localconfig_content)
    else:
        # Need to add it - find the Software section
        software_match = re.search(r'(\s*"Software"\s*{)', localconfig_content)
        if software_match:
            insert_pos = software_match.end()
            new_content = (
                localconfig_content[:insert_pos] +
                f'\n\t\t{replacement}\n' +
                localconfig_content[insert_pos:]
            )
        else:
            logger.warning("Could not find Software section in localconfig.vdf")
            return localconfig_content
    
    return new_content


def find_or_create_collection_by_name(collections, leveldb_collections, collection_name, appids):
    """
    Find an existing collection by name from LevelDB, or create a new one.

    Priority:
    1. If LevelDB has this collection name, use that collection ID (even if empty)
    2. Otherwise, try to find a collection by matching game AppIDs (heuristic fallback)
    3. If still not found, create a new collection

    Args:
        collections: Existing collections from localconfig.vdf (ID -> data)
        leveldb_collections: Collections from LevelDB (ID -> name)
        collection_name: Name of the collection to find/create
        appids: List of AppIDs we want to add to this collection

    Returns:
        (collection_id, is_new) tuple
    """
    # PRIORITY 1: Check LevelDB for this collection name
    if leveldb_collections:
        for coll_id, name in leveldb_collections.items():
            if name.lower() == collection_name.lower():
                # Found it in LevelDB! Make sure it exists in localconfig too
                if coll_id in collections:
                    logger.info(f"Found existing collection '{collection_name}' with ID {coll_id} (from LevelDB)")
                    return coll_id, False
                else:
                    # Collection exists in LevelDB but not in localconfig - add it
                    logger.warning(f"Collection '{collection_name}' found in LevelDB but not in localconfig")
                    logger.warning(f"Adding it to localconfig with ID {coll_id}")
                    collections[coll_id] = {
                        "id": coll_id,
                        "added": [],
                        "removed": []
                    }
                    return coll_id, False

    # PRIORITY 2: LevelDB didn't have it - try heuristic matching by game AppIDs
    # This handles cases where LevelDB couldn't be read
    appid_set = set(appids)
    best_match_id = None
    best_match_ratio = 0

    for coll_id, coll_data in collections.items():
        if not isinstance(coll_data, dict):
            continue

        collection_appids = set(coll_data.get('added', []))
        if not collection_appids:
            continue

        matches = len(appid_set & collection_appids)
        ratio = matches / len(appid_set) if appid_set else 0

        if ratio > best_match_ratio:
            best_match_ratio = ratio
            best_match_id = coll_id

    # If we found a collection with at least 50% of our games, use it
    if best_match_ratio >= 0.5:
        logger.info(f"Found existing collection for '{collection_name}' with {int(best_match_ratio * 100)}% match: {best_match_id} (heuristic)")
        return best_match_id, False

    # PRIORITY 3: Create a new collection
    new_id = generate_collection_id()
    logger.info(f"Creating new collection '{collection_name}' with ID {new_id}")
    logger.info(f"This collection will appear in Steam after you open it")

    collections[new_id] = {
        "id": new_id,
        "added": [],
        "removed": []
    }

    return new_id, True


def update_localconfig_collections(localconfig_path, appids, collection_names=None, steam_user_data_path=None):
    """
    Add non-Steam games to collections in localconfig.vdf.

    For each collection name provided:
    - If it exists in LevelDB: Use it
    - If it doesn't exist: Create it in localconfig.vdf

    Args:
        localconfig_path: Path to localconfig.vdf
        appids: List of AppIDs (as integers) to add
        collection_names: List of collection names to add games to (from collections parameter)
        steam_user_data_path: Path to Steam userdata for LevelDB verification

    Returns:
        List of collection IDs used
    """
    if not collection_names:
        collection_names = ["Non-Steam"]

    try:
        # Create backup before modifying
        backup_path = localconfig_path + '.backup'
        import shutil
        shutil.copy2(localconfig_path, backup_path)
        logger.info(f"Created backup: {backup_path}")

        # Read localconfig
        with open(localconfig_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        # Extract existing collections
        collections = extract_user_collections(content)

        logger.info(f"Found {len(collections)} existing collections in localconfig.vdf")

        # Try to read collections from cloud storage
        leveldb_collections = read_cloud_storage_collections(steam_user_data_path)

        # Process each collection name from default_tags
        used_collection_ids = []
        new_collections_created = []

        for collection_name in collection_names:
            logger.info(f"\nProcessing collection: '{collection_name}'")

            # Find or create the collection
            collection_id, is_new = find_or_create_collection_by_name(
                collections, leveldb_collections, collection_name, appids
            )

            if is_new:
                new_collections_created.append((collection_id, collection_name))

            # Add all games to this collection
            update_collection_with_appids(collections, collection_id, appids)
            used_collection_ids.append(collection_id)

            logger.info(f"Added {len(appids)} games to collection '{collection_name}' ({collection_id})")

        # Write back to localconfig
        new_content = write_user_collections(content, collections)

        with open(localconfig_path, 'w', encoding='utf-8') as f:
            f.write(new_content)

        logger.info(f"\n✓ Updated localconfig.vdf with {len(used_collection_ids)} collection(s)")

        # Warn about new collections that need Steam to sync
        if new_collections_created:
            logger.warning("\n" + "=" * 70)
            logger.warning("NEW COLLECTIONS CREATED")
            logger.warning("=" * 70)
            for coll_id, coll_name in new_collections_created:
                logger.warning(f"  - '{coll_name}' ({coll_id})")
            logger.warning("")
            logger.warning("These collections exist in localconfig.vdf but not yet in LevelDB.")
            logger.warning("To complete the setup:")
            logger.warning("  1. Open Steam")
            logger.warning("  2. Go to Library > Collections")
            logger.warning("  3. You should see the new collection(s)")
            logger.warning("  4. Steam will automatically sync them to LevelDB")
            logger.warning("=" * 70)

        return used_collection_ids

    except Exception as e:
        logger.error(f"Error updating localconfig.vdf: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        # Restore backup if something went wrong
        if os.path.exists(backup_path):
            shutil.copy2(backup_path, localconfig_path)
            logger.info("Restored backup due to error")
        return None

# ============================================================================
# END LOCALCONFIG.VDF FUNCTIONS
# ============================================================================

    
# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

storedParametersJSONFilename = "parameters.json"

storedParametersJSON = {}
storedParametersJSON = readJsonFile(storedParametersJSONFilename)

game_installation_path = ""
steamgriddb_api_key = ""
steamdir_path = ""
collections = []

##Taking them from the JSON if it exists
if storedParametersJSON:
    game_installation_path = storedParametersJSON["game_installation_path"]
    steamgriddb_api_key = storedParametersJSON["steamgriddb_api_key"]
    steamdir_path = storedParametersJSON["steamdir_path"]
    collections = storedParametersJSON.get("collections", ["Non-Steam"])

totalGames = 0
currentGame = 0


def read_current_games():
    """Read the current games from the game installation directory."""
    try:
        current_games = { os.path.join(base_path, subfolder)
    for base_path in game_installation_path.split(";")
    if os.path.isdir(base_path)
    for subfolder in os.listdir(base_path)
    if os.path.isdir(os.path.join(base_path, subfolder))}
        global totalGames
        totalGames = len(current_games)
        logger.info(f"Total number of games: {totalGames}")



    except Exception as e:
        logger.error(f"Error reading game installation directory {game_installation_path}: {e}")
        return set()
    return current_games

def generate_appid(game_name, exe_path):
    """Generate a unique appid for the game based on its exe path and name."""
    unique_name = (exe_path + game_name).encode('utf-8')
    legacy_id = zlib.crc32(unique_name) | 0x80000000
    return str(legacy_id)

def getGridImageURLBySize(json,image_type):
    imageSize = {'grid' : (600,900), 'home' : (920,430)}

    ##Locating the image type by size, and taking the first result 
    url = [x for x in json['data'] if x and x['width'] == imageSize[image_type][0] and x['height'] == imageSize[image_type][1]]
    if(url):
        return url[0]['url']
    else:
        return None

def fetch_steamgriddb_image(game_id, image_type):
    """Fetch a single image (first available) of specified type from SteamGridDB."""
    headers = {
        'Authorization': f'Bearer {steamgriddb_api_key}'
    }
    if image_type == 'hero':
        base_url = f'https://www.steamgriddb.com/api/v2/heroes/game/{game_id}'
    elif image_type == "home":
        base_url = f'https://www.steamgriddb.com/api/v2/grids/game/{game_id}'
    else: base_url = f'https://www.steamgriddb.com/api/v2/{image_type}s/game/{game_id}'
    response = requests.get(base_url, headers=headers)
    logger.info(f"Fetching {image_type} for game ID: {game_id}, URL: {base_url}, Status Code: {response.status_code}")


    if response.status_code == 200:
        data = response.json()
        if data['success'] and data['data']:
            if image_type == "home" or image_type == 'grid':
                return getGridImageURLBySize(data, image_type)
            return data['data'][0]['url']  # Return the URL of the first image found
        
    logger.error(f"Failed to fetch {image_type} for game ID: {game_id}")
    return None

def download_image(url, local_path):
    """Download an image from URL and save it locally."""
    try:
        response = requests.get(url)
        if response.status_code == 200:
            with open(local_path, 'wb') as f:
                f.write(response.content)
            logger.info(f"Downloaded image from {url} to {local_path}")
            return True
    except Exception as e:
        logger.error(f"Failed to download image from {url}: {e}")
    return False

def save_images(appid, game_id):
    """Save grid, hero, and logo images for the game."""
    image_types = ['grid', 'hero', 'logo', 'home']
    for image_type in image_types:
        url = fetch_steamgriddb_image(game_id, image_type)
        if url:
            extension = os.path.splitext(url)[1]

            if image_type == 'grid':
                image_path = os.path.join(grid_folder, f'{appid}p{extension}')
            elif image_type == 'hero':
                image_path = os.path.join(grid_folder, f'{appid}_hero{extension}')
            elif image_type == 'logo':
                image_path = os.path.join(grid_folder, f'{appid}_logo{extension}')
            elif image_type == 'home':
                image_path = os.path.join(grid_folder, f'{appid}{extension}')

            logger.info(f"Saving {image_type} image for appid {appid} from {url} to {image_path}")
            if not os.path.exists(image_path):
                if download_image(url, image_path):
                    logger.info(f"Downloaded {image_type} image for appid {appid} from {url}")

def find_largest_exe(game_dir):
    largest_file = None
    largest_size = 0
    #To avoid the uninstaller or some other exe to be used, mostly in Unity games
    exceptions = ["unins","unity","redist"]

    for root, dirs, files in os.walk(game_dir):
        for file in files:
            if file.endswith(".exe") and not any([x in file.lower() for x in exceptions]):
                file_path = os.path.join(root, file)
                file_size = os.path.getsize(file_path)
                if file_size > largest_size:
                    largest_size = file_size
                    largest_file = file_path
    
    return largest_file



def update_shortcuts(current_games):
    """Update the Steam shortcuts with new and removed games, and fetch/update images."""

    steam_user_data_path = determineUserdataFolder()

    global grid_folder
    grid_folder = os.path.join(steam_user_data_path, 'grid')  # Folder to store grid images
    # Ensure the grid folder exists
    Path(grid_folder).mkdir(parents=True, exist_ok=True)

    shortcuts_file = os.path.join(steam_user_data_path, 'shortcuts.vdf')
    localconfig_file = os.path.join(steam_user_data_path, 'localconfig.vdf')

    try:
        # Read existing shortcuts file if it exists
        if os.path.exists(shortcuts_file):
            with open(shortcuts_file, 'rb') as f:
                shortcuts = vdf.binary_load(f)
        else:
            shortcuts = {'shortcuts': {}}

        # Collect the current shortcuts
        existing_games = {os.path.basename(shortcut.get('StartDir', '').lower().strip('"')): shortcut for shortcut in shortcuts['shortcuts'].values()}

        # Get set of current game names (not paths) for comparison
        current_game_names = {os.path.basename(game_path).lower() for game_path in current_games}

        # Remove shortcuts for games no longer in the installation directory
        for game_name, shortcut in existing_games.items():
            if game_name not in current_game_names:
                appid = shortcut.get('appid', '')
                # Remove images associated with the game
                for image_type in ['p', '_hero', '_logo','home']:
                    for ext in ['.jpg', '.png']:
                        if(image_type == 'home'):
                            image_path = os.path.join(grid_folder, f'{appid}{ext}')
                        else:
                            image_path = os.path.join(grid_folder, f'{appid}{image_type}{ext}')
                        if os.path.exists(image_path):
                            os.remove(image_path)
                            logger.info(f"Removed {image_type} image for game: {game_name}")

                # Remove the shortcut from shortcuts file
                for idx, s in list(shortcuts['shortcuts'].items()):
                    if s.get('appname', '').strip().lower() == game_name:
                        del shortcuts['shortcuts'][idx]
                        logger.info(f"Removed shortcut for game: {game_name}")

        # Track AppIDs for collection update
        all_appids = []

        # Add or update games in shortcuts
        for game_path in current_games:
            game_name = None  # Initialize to avoid undefined variable in exception handler
            try:
                game_name = os.path.basename(game_path)
                game_name_lower = game_name.lower()
                
                global currentGame
                currentGame += 1
                logger.info("")
                logger.info(f"Current game: {game_name}")
                logger.info(f"Games processed: {currentGame}/{totalGames}")

                if game_name_lower in existing_games:
                    logger.info(f"{game_name} already in Steam")
                    # Still add to collection list
                    existing_appid = int(existing_games[game_name_lower].get('appid', 0))
                    if existing_appid:
                        all_appids.append(existing_appid)
                    continue

                exe_file = find_largest_exe(game_path)
                if exe_file:
                    logger.info(f"Largest .exe file found: {exe_file}")
                else:
                    logger.error(f"No .exe files found for {game_name}. Skipping...")
                    continue
                
                # exe_file is already the full path from find_largest_exe
                exe_path = exe_file
                
                appid_int = int(generate_appid(game_name, exe_path))
                appid = str(appid_int)
                all_appids.append(appid_int)  # Track for collections
                
                # Search for the game on SteamGridDB and fetch images
                headers = {
                    'Authorization': f'Bearer {steamgriddb_api_key}'
                }
                search_url = f'https://www.steamgriddb.com/api/v2/search/autocomplete/{game_name}'
                response = requests.get(search_url, headers=headers)
                logger.info(f"Searching SteamGridDB for {game_name}, URL: {search_url}, Status Code: {response.status_code}")
                
                # Use the original game_name for the shortcut, but fetch images with the matched name
                shortcut_game_name = game_name
                
                if response.status_code == 200:
                    data = response.json()
                    if data['success'] and data['data']:
                        game_id = data['data'][0]['id']  # Assuming first result is the best match
                        matched_name = data['data'][0]['name']
                        logger.info(f"SteamGridDB matched '{game_name}' to '{matched_name}'")
                        # Use the matched name for better display, but keep original for file paths
                        shortcut_game_name = matched_name
                        save_images(appid, game_id)

                # Add shortcut entry
                new_entry = {
                    "appid": appid,
                    "appname": shortcut_game_name,
                    "exe": f'"{exe_path}"',
                    "StartDir": f'"{game_path}"',
                    "icon": "",
                    "ShortcutPath": "",
                    "LaunchOptions": "",
                    "IsHidden": 0,
                    "AllowDesktopConfig": 1,
                    "AllowOverlay": 1,
                    "OpenVR": 0,
                    "Devkit": 0,
                    "DevkitGameID": "",
                    "LastPlayTime": 0,
                    "tags": {}
                }
                shortcuts['shortcuts'][str(len(shortcuts['shortcuts']))] = new_entry
                logger.info(f"Added shortcut for game: {shortcut_game_name}")
                
            except Exception as e:
                error_game = game_name if game_name else os.path.basename(game_path)
                logger.error(f"Error processing game {error_game}: {e}. Continuing with next game...")
                continue

        # Save the updated shortcuts file
        with open(shortcuts_file, 'wb') as f:
            vdf.binary_dump(shortcuts, f)
            logger.info("Shortcuts file updated and saved.")

        # Update localconfig.vdf with collections
        if all_appids and os.path.exists(localconfig_file):
            logger.info(f"\nUpdating collections in localconfig.vdf for {len(all_appids)} games...")

            # Use collections from parameters.json as collection names
            collection_names = collections if collections else ["Non-Steam"]
            logger.info(f"Target collections: {', '.join(collection_names)}")

            update_localconfig_collections(localconfig_file, all_appids, collection_names, steam_user_data_path)
            logger.info("\n✓ Collections updated! Restart Steam to see changes.")
        else:
            if not all_appids:
                logger.warning("No AppIDs to add to collections")
            if not os.path.exists(localconfig_file):
                logger.warning(f"localconfig.vdf not found at {localconfig_file}")

    except Exception as e:
        logger.error(f"Error updating shortcuts: {e}")


def GUI():

    parser = GooeyParser(description='Get your NonSteam Games added on Steam.')

    parser.add_argument(
        'game_installation_path',
        widget='MultiDirChooser',
        metavar='NonSteam Games Folder',
        action='store',
        default = game_installation_path if game_installation_path else ''
        )
    
    parser.add_argument(
        'steamgriddb_api_key',
        metavar='SteamgridDB API Key',
        help='You can get yours in the link below:\nhttps://www.steamgriddb.com/profile/preferences/api\nSubstitute the link with the key after you have generated it',
        default = steamgriddb_api_key if steamgriddb_api_key else "https://www.steamgriddb.com/profile/preferences/api",
        action='store'
        )
    
    parser.add_argument(
        'steamdir_path',
        metavar='Steam Installation Path',
        widget='DirChooser',
        help = "By default C:\\Program Files (x86)\\Steam",
        default=steamdir_path if steamdir_path else 'C:\\Program Files (x86)\\Steam',
        action='store'
        )

    parser.add_argument(
        'collections',
        metavar='Collections (separated by ;)',
        help='Collection names to organize your non-Steam games into. Multiple collections can be specified separated by semicolons.',
        action='store',
        default = ';'.join(collections) if collections else ''
        )

    return parser.parse_args()

def storeVariablesFromGUI(args):
    global game_installation_path, steamgriddb_api_key, steamdir_path, collections

    game_installation_path = args.game_installation_path
    steamgriddb_api_key = args.steamgriddb_api_key
    steamdir_path = args.steamdir_path

    # Convert semicolon-separated string to list
    if args.collections:
        collections = [coll.strip() for coll in args.collections.split(';')]
    else:
        collections = []

    storedParametersJSON = {}
    storedParametersJSON["game_installation_path"] = game_installation_path
    storedParametersJSON["steamgriddb_api_key"] = steamgriddb_api_key
    storedParametersJSON["steamdir_path"] = steamdir_path
    storedParametersJSON["collections"] = collections  # Store as list

    saveJsonFile(storedParametersJSONFilename, storedParametersJSON)

def main():
    """Main function to check for new or removed games and update Steam shortcuts accordingly."""
    try:
        # Check if running with --ignore-gooey (use already-loaded stored config)
        if '--ignore-gooey' not in sys.argv:
            # Only run GUI if NOT using --ignore-gooey
            argumentsGUI = GUI()
            storeVariablesFromGUI(argumentsGUI)
            
        # Verify we have the required parameters
        if not game_installation_path or not steamgriddb_api_key or not steamdir_path:
            logger.error("Missing required parameters. Please run with GUI first to set them up.")
            return

        # Check if Steam is running
        if check_steam_running():
            logger.error("Aborting - Steam must be closed first!")
            return

        logger.info("Reading current games from installation directory...")
        current_games = read_current_games()

        #Workaround since backslash are not allowed in f-strings
        nl = '\n'
        #Parsed to have a game per line
        logger.info(f"Current games: {nl.join(str(current_games).split(','))}")

        determineUserdataFolder()

        logger.info("Updating shortcuts and fetching images...")
        update_shortcuts(current_games)

    except Exception as e:
        logger.error(f"Unexpected error in main function: {e}")

# Apply Gooey decorator conditionally
if '--ignore-gooey' not in sys.argv:
    main = Gooey(
        show_preview_warning=False, 
        progress_regex=r"Games processed: (?P<current>\d+)/(?P<total>\d+)$", 
        progress_expr="current / total * 100"
    )(main)

if __name__ == "__main__":
    main()