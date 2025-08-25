import os
import json
import time
import random
import re
import requests
from datetime import datetime, timezone
from itertools import product
from urllib.parse import urlparse

import xml.etree.ElementTree as ET
import dotenv
import argparse
import concurrent.futures
from email.utils import formatdate

try:

    if not os.environ.get('GITHUB_ACTIONS', '').lower() == 'true':
        dotenv.load_dotenv('.env')
except ImportError:
    pass

CONFIG_FILE = 'config.json'
CAPTURES_DIR = 'rss'
DELAY_RANGE = (1, 3)  # seconds, shorter to keep jobs fast
MAX_RETRIES = 3
BACKOFF_FACTOR = 2
REQUEST_TIMEOUT = 10  # seconds

def is_safe_path(base_dir, path):
    base_dir = os.path.realpath(base_dir)
    path = os.path.realpath(path)
    return os.path.commonpath([base_dir]) == os.path.commonpath([base_dir, path])

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def substitute(template, variables):
    variables = dict(variables)
    variables['today'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    def replacer(match):
        var = match.group(1)
        return str(variables.get(var, match.group(0)))
    return re.sub(r'\$?\{([^}]+)\}', replacer, template)

def expand_targets(defs, targets):
    # Separate fixed and list variables from defs
    fixed_defs = {k: v for k, v in defs.items() if not isinstance(v, list)}
    list_defs = {k: v for k, v in defs.items() if isinstance(v, list)}

    # Heuristic mapping for template variable names (e.g., "langs" -> "lang")
    list_var_template_names_map = {
        key: key[:-1] if key.endswith('s') and len(key) > 1 else key
        for key in list_defs.keys()
    }

    all_expanded_targets = []

    # Generate all combinations from defs list variables
    defs_combinations = []
    if list_defs:
        list_product_keys = sorted(list(list_defs.keys())) # Ensure consistent order
        list_product_values = [list_defs[key] for key in list_product_keys]
        for combo_values in product(*list_product_values):
            combo_dict = {}
            for i, list_key in enumerate(list_product_keys):
                template_var_name = list_var_template_names_map.get(list_key, list_key)
                combo_dict[template_var_name] = combo_values[i]
            defs_combinations.append(combo_dict)
    else:
        defs_combinations.append({}) # No list variables in defs, so just one empty combination

    for defs_combo in defs_combinations:
        # Construct the base variables for the current defs combination
        current_base_vars_for_base_sub = {**fixed_defs, **defs_combo}
        current_base_vars = {**current_base_vars_for_base_sub}
        # Substitute 'base' now that all defs variables are available for this combination
        current_base_vars['base'] = substitute(defs.get('base', ''), current_base_vars_for_base_sub)

        for target in targets:
            # Separate fixed and list variables from target vars
            target_vars = target.get('vars', {})
            fixed_target_vars = {k: v for k, v in target_vars.items() if not isinstance(v, list)}
            list_target_vars = {k: v for k, v in target_vars.items() if isinstance(v, list)}

            # Generate all combinations from target list variables
            target_combinations = []
            if list_target_vars:
                target_list_product_keys = sorted(list(list_target_vars.keys())) # Ensure consistent order
                target_list_product_values = [list_target_vars[key] for key in target_list_product_keys]
                for combo_values in product(*target_list_product_values):
                    combo_dict = dict(zip(target_list_product_keys, combo_values))
                    target_combinations.append(combo_dict)
            else:
                target_combinations.append({}) # No list variables in target, so just one empty combination

            for target_combo in target_combinations:
                # Combine all variables for the final substitution
                all_vars = {
                    **current_base_vars,
                    **fixed_target_vars,
                    **target_combo
                }

                filepath = substitute(target.get('filepath', ''), all_vars)
                url = substitute(target.get('url', ''), all_vars)

                # Extract language for grouping if present in all_vars
                lang = all_vars.get(list_var_template_names_map.get('langs', 'langs'))

                expanded_entry = {'filepath': filepath, 'url': url}
                if lang:
                    expanded_entry['lang'] = lang
                all_expanded_targets.append(expanded_entry)

    return all_expanded_targets

ALLOWED_DOMAINS = {"https://www.producthunt.com"}

def process_language_targets(language_targets, archive_base_url=""):
    """Processes a list of targets for a single language sequentially."""
    language_failures = []
    for entry in language_targets:
        if 'filepath' not in entry:
            print(f"[ERROR] Main loop: Entry missing 'filepath': {entry}")
            language_failures.append({"url": entry.get('url', '[NO URL]'), "filepath": "[MISSING FILEPATH]", "error": "missing filepath"})
            continue
        filepath = os.path.join(CAPTURES_DIR, entry['filepath'])
        if not is_safe_path(CAPTURES_DIR, filepath):
            print(f"[ERROR] Unsafe filepath detected: {filepath}")
            language_failures.append({"url": entry.get('url', '[NO URL]'), "filepath": filepath, "error": "unsafe filepath"})
            continue
        url = entry.get('url', '[NO URL]')
        if url == '[NO URL]':
            print(f"[ERROR] Main loop: Entry missing 'url' for filepath: {filepath}")
            language_failures.append({"url": "[MISSING URL]", "filepath": filepath, "error": "Missing URL in config entry"})
            continue

        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            print(f"[INFO] Skipping {url} -> {filepath} (file exists and is non-empty)")
            continue

        print(f"[INFO] Processing {url} -> {filepath}")
        content = fetch_url(url)
        if content:
            ## check if the content is valid xml
            try:
                ET.fromstring(content)
            except ET.ParseError:
                print(f"[ERROR] Invalid XML content for {url}")
                language_failures.append({"url": url, "filepath": filepath, "error": "invalid XML"})
                continue
            
            # Transform Atom to RSS 2.0 format with archive links
            transformed_content = transform_atom_to_rss(content, archive_base_url=archive_base_url)
            
            if transformed_content:
                save_content(os.path.dirname(filepath), os.path.basename(filepath), transformed_content)
                print(f"[INFO] Saved transformed RSS content for {url} to {filepath}")
            else:
                print(f"[ERROR] Failed to transform RSS content for {url}")
                language_failures.append({"url": url, "filepath": filepath, "error": "RSS transformation failed"})
        else:
            print(f"[ERROR] Failed to fetch content for {url}")
            language_failures.append({"url": url, "filepath": filepath, "error": "fetch failed"})

        delay = random.uniform(*DELAY_RANGE)
        print(f"[INFO] Sleeping for {delay:.2f} seconds...")
        time.sleep(delay)
    return language_failures

def fetch_url(url):
    headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1 OPT/5.0.5"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.text
            elif resp.status_code in (429, 500, 502, 503, 504):
                print(f"[WARN] Retrying {url} due to status {resp.status_code} (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(BACKOFF_FACTOR ** attempt)
            else:
                print(f"[ERROR] Non-retryable error {resp.status_code} for {url}")
                return None
        except requests.exceptions.RequestException as e_req:
            print(f"[ERROR] RequestException during fetch for {url} (attempt {attempt}/{MAX_RETRIES}): {e_req}")
            time.sleep(BACKOFF_FACTOR ** attempt)
    print(f"[ERROR] Failed to fetch {url} after {MAX_RETRIES} attempts.")
    return None

def transform_atom_to_rss(atom_content, archive_base_url=""):
    """Transform Atom feed to RSS 2.0 format and replace external links with archive links."""
    try:
        # Parse the Atom feed
        root = ET.fromstring(atom_content)
        
        # Handle namespace prefixes - remove them for cleaner output
        namespace_map = {'atom': 'http://www.w3.org/2005/Atom'}
        
        # Create RSS 2.0 structure
        rss = ET.Element('rss', version='2.0')
        channel = ET.SubElement(rss, 'channel')
        
        # Extract feed-level information
        title_elem = root.find('.//{http://www.w3.org/2005/Atom}title')
        feed_title = title_elem.text if title_elem is not None else "Product Hunt Archive"
        
        link_elem = root.find('.//{http://www.w3.org/2005/Atom}link[@rel="alternate"]')
        feed_link = link_elem.get('href') if link_elem is not None else archive_base_url
        
        updated_elem = root.find('.//{http://www.w3.org/2005/Atom}updated')
        last_build_date = ""
        if updated_elem is not None:
            try:
                # Convert from ISO format to RFC 2822 format
                from datetime import datetime
                dt = datetime.fromisoformat(updated_elem.text.replace('Z', '+00:00'))
                last_build_date = formatdate(dt.timestamp())
            except:
                last_build_date = formatdate()
        else:
            last_build_date = formatdate()
        
        # Add channel elements
        ET.SubElement(channel, 'title').text = feed_title
        ET.SubElement(channel, 'link').text = archive_base_url or feed_link
        ET.SubElement(channel, 'description').text = "Product Hunt daily archive"
        ET.SubElement(channel, 'lastBuildDate').text = last_build_date
        ET.SubElement(channel, 'generator').text = "ph-archive"
        
        # Process entries
        entries = root.findall('.//{http://www.w3.org/2005/Atom}entry')
        
        for entry in entries:
            item = ET.SubElement(channel, 'item')
            
            # Title
            title_elem = entry.find('.//{http://www.w3.org/2005/Atom}title')
            if title_elem is not None:
                ET.SubElement(item, 'title').text = title_elem.text
            
            # Link - replace with archive link if archive_base_url is provided
            link_elem = entry.find('.//{http://www.w3.org/2005/Atom}link[@rel="alternate"]')
            original_link = link_elem.get('href') if link_elem is not None else ""
            
            if archive_base_url and original_link:
                # Extract post ID from ProductHunt URL for archive link
                import re
                post_match = re.search(r'/posts/([^/?]+)', original_link)
                if post_match:
                    post_slug = post_match.group(1)
                    archive_link = f"{archive_base_url}/posts/{post_slug}"
                    ET.SubElement(item, 'link').text = archive_link
                else:
                    ET.SubElement(item, 'link').text = original_link
            else:
                ET.SubElement(item, 'link').text = original_link
            
            # Description/Content
            content_elem = entry.find('.//{http://www.w3.org/2005/Atom}content')
            if content_elem is not None:
                content = content_elem.text or ""
                # Replace ProductHunt links in content with archive links if needed
                if archive_base_url:
                    content = re.sub(
                        r'href="https://www\.producthunt\.com/posts/([^"?]+)[^"]*"',
                        f'href="{archive_base_url}/posts/\\1"',
                        content
                    )
                ET.SubElement(item, 'description').text = content
            
            # Publication date
            published_elem = entry.find('.//{http://www.w3.org/2005/Atom}published')
            if published_elem is not None:
                try:
                    dt = datetime.fromisoformat(published_elem.text.replace('Z', '+00:00'))
                    pub_date = formatdate(dt.timestamp())
                    ET.SubElement(item, 'pubDate').text = pub_date
                except:
                    pass
            
            # Author
            author_elem = entry.find('.//{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name')
            if author_elem is not None:
                ET.SubElement(item, 'author').text = author_elem.text
            
            # GUID
            id_elem = entry.find('.//{http://www.w3.org/2005/Atom}id')
            if id_elem is not None:
                ET.SubElement(item, 'guid').text = id_elem.text
        
        # Convert to string with proper XML declaration
        ET.indent(rss, space="  ")
        xml_str = ET.tostring(rss, encoding='unicode')
        return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_str}'
        
    except Exception as e:
        print(f"[ERROR] Failed to transform Atom to RSS: {e}")
        return None

def save_content(folder, filename, content):
    os.makedirs(folder, exist_ok=True)
    file_path = os.path.join(folder, filename)
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)

def generate_folders(defs, targets):
    expanded = expand_targets(defs, targets)
    for entry in expanded:
        if 'filepath' not in entry:
            print(f"[ERROR] Entry missing 'filepath': {entry}")
            continue
        filepath = os.path.join(CAPTURES_DIR, entry['filepath'])
        print(f"[INFO] Creating parent directory for: {filepath}")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

def send_telegram_message(message):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print("Telegram bot token or chat ID not set; skipping notification.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=data, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"Failed to send Telegram message: {resp.text}")
    except Exception as e:
        print(f"Exception sending Telegram message: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Print expanded URLs and paths, then exit')
    parser.add_argument('--test', action='store_true', help='Test mode: process only a subset of items')
    parser.add_argument('--random', action='store_true', help='Shuffle items before processing (only active if --test is also specified)')
    parser.add_argument('--number', type=int, default=1, help='Number of items to process in test mode (default: 1)')
    args = parser.parse_args()

    config = load_config()
    defs = config.get('defs', {})
    targets = config.get('target', [])
    archive_base_url = defs.get('archive_base_url', '')

    # Generate folders for all potential targets initially
    generate_folders(defs, targets)
    expanded = expand_targets(defs, targets)

    if args.test:
        if args.random:
            print("[INFO] Test mode: Randomizing target list.")
            random.shuffle(expanded)
        else:
            print("[INFO] Test mode: Using first N targets.")
        if args.number > len(expanded):
            print(f"[WARN] Requested number ({args.number}) is more than available targets ({len(expanded)}). Processing all available.")
            args.number = len(expanded)
        expanded = expanded[:args.number]
        print(f"[INFO] Test mode: Processing {len(expanded)} item(s).")

    if args.dry_run:
        print('[INFO] Dry run: expanded URLs and paths to be processed:')
        if not expanded:
            print("[INFO] No items selected for dry run.")
        for entry in expanded:
            if 'filepath' not in entry:
                print(f"[ERROR] Dry run: Entry missing 'filepath': {entry}")
                continue
            filepath = os.path.join(CAPTURES_DIR, entry['filepath'])
            url = entry.get('url', '[NO URL]')
            lang = entry.get('lang', '[NO LANG]')
            print(f"[LANG: {lang}] {url} -> {filepath}")
        return

    failures = []
    if not expanded:
        print("[INFO] No items to process.")

    # Group targets by language
    language_groups = {}
    for entry in expanded:
        lang = entry.get('lang', 'default')  # Use 'default' if no language is specified
        if lang not in language_groups:
            language_groups[lang] = []
        language_groups[lang].append(entry)

    # Process each language group in parallel using ThreadPoolExecutor
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_lang = {
            executor.submit(process_language_targets, targets_for_lang, archive_base_url):
            lang for lang, targets_for_lang in language_groups.items()
        }
        for future in concurrent.futures.as_completed(future_to_lang):
            lang = future_to_lang[future]
            try:
                language_failures = future.result()
                failures.extend(language_failures)
                print(f"[INFO] Finished processing language group: {lang}")
            except Exception as exc:
                print(f"[ERROR] Language group {lang} generated an exception: {exc}")
                failures.append({"language": lang, "error": str(exc), "type": "thread_exception"})

    if failures:
        msg_lines = [f"*Capture Failures* ({datetime.now(timezone.utc).isoformat()} UTC):"]
        for f in failures:
            msg_lines.append(f"- `{f.get('url', f.get('language', 'unknown'))}` for `{f.get('filepath', 'unknown')}`: {f['error']}")
        send_telegram_message('\n'.join(msg_lines))

    print("[INFO] Script finished.")

if __name__ == '__main__':
    main()
