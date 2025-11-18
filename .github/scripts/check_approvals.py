import os
import sys
import json
import requests
import logging
from pathlib import Path
import shlex

logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stdout)

TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_EVENT_PATH = os.environ.get('GITHUB_EVENT_PATH', '')
GITHUB_REPOSITORY = os.environ.get('GITHUB_REPOSITORY', '')
HEADERS = {
    'Authorization': f'token {TOKEN}',
    'Accept': 'application/vnd.github.v3+json'
}
CODEOWNERS_FILE = '.github/CODEOWNERS-DWH'


def fail(message):
    logging.error(f"::error::{message}")
    sys.exit(1)


def make_request(url):
    try:
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status() 
        data = response.json()
        headers = response.headers
        return data, headers
    except requests.exceptions.HTTPError as e:
        raise Exception(f"HTTPError when requesting {url}: {e.response.status_code} {e.response.reason}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed during request to {url}: {e}")


def get_next_page(headers):
    link_header = headers.get('Link')
    if not link_header:
        return None
    links = link_header.split(',')
    for link in links:
        parts = link.split(';')
        if len(parts) == 2:
            try:
                url_part = parts[0].strip()[1:-1]
                rel_part = parts[1].strip()
                if rel_part == 'rel="next"':
                    return url_part
            except Exception:
                continue
    return None


def get_pr_context(repo_full_str, event_path_str):
    try:
        owner, repo = repo_full_str.split('/')
        with open(event_path_str, 'r') as f:
            event_data = json.load(f)
        pr_number = event_data['pull_request']['number']
        return owner, repo, pr_number
    except FileNotFoundError:
        fail(f"Failed to read event path: {event_path_str}")
    except (AttributeError, TypeError, KeyError):
        fail("Failed to parse event data. GITHUB_REPOSITORY or GITHUB_EVENT_PATH seem invalid.")
    except Exception as e:
        fail(f"Failed to get PR context: {e}")


def parse_codeowners(filepath):
    rules = []
    try:
        with open(filepath, 'r') as f:
            content = f.read()

        # Support multi-line rules by joining lines that end with a '\'
        # This allows for a more readable CODEOWNERS-DWH file.
        # Example: "* @owner1 \ \n   @owner2" becomes "* @owner1 @owner2"
        processed_content = content.replace('\\\n', ' ')

        for line_number, line in enumerate(processed_content.splitlines(), 1):
            line = line.split('#', 1)[0].strip()
            if not line:
                continue

            try:
                # Safely parse paths with spaces
                parts = shlex.split(line)
            except ValueError as e:
                logging.warning(f"Skipping malformed line {line_number}: {line} | Error: {e}")
                continue
            if not parts:
                continue

            raw_pattern = parts[0]
            owner_strings = parts[1:]
            patterns_to_check = []

            if raw_pattern == '*':
                patterns_to_check.append('*')
            elif raw_pattern == '**':
                patterns_to_check.append('**')
            elif raw_pattern.startswith('/'):
                patterns_to_check.append(raw_pattern[1:])  # Root-anchored path
            else:
                patterns_to_check.append(raw_pattern)
                patterns_to_check.append(f"**/{raw_pattern}")

            final_patterns = []
            for p in patterns_to_check:
                # Edge case: '.../**' must become '.../**/*' to match files
                if p.endswith('/**') and p != '**':
                    final_patterns.append(f"{p}/*")
                else:
                    final_patterns.append(p)

            # This script intentionally ignores team owners (@org/team)
            owners = {o.replace('@', '') for o in owner_strings if '/' not in o}
            if not owners:
                logging.warning(f"No owners found for pattern on line {line_number}: {raw_pattern}")
                continue

            # Store a list of patterns for each rule
            rules.append({'patterns': final_patterns, 'owners': owners})

    except FileNotFoundError:
        fail(f"Failed to read {filepath}: File not found.")
    except Exception as e:
        fail(f"Failed to parse {filepath}: {e}")
    
    return rules


def get_changed_files(owner, repo, pr_number):
    all_files = []
    next_url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100'
    
    while next_url:
        logging.info(f"Fetching changed files from: {next_url}")
        data, headers = make_request(next_url)
        
        all_files.extend([Path(f['filename']) for f in data])
        
        next_url = get_next_page(headers)
        
    logging.info(f"Total files found: {len(all_files)}")
    return all_files


def get_approved_users(owner, repo, pr_number):
    all_approved_users = set()
    next_url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews?per_page=100'
    
    while next_url:
        logging.info(f"Fetching reviews from: {next_url}")
        data, headers = make_request(next_url)
        
        approved_in_page = {
            review['user']['login']
            for review in data
            if review['state'] == 'APPROVED'
        }
        all_approved_users.update(approved_in_page)
        
        next_url = get_next_page(headers)
    
    logging.info(f"Found approvals from: {', '.join(all_approved_users) if all_approved_users else 'None'}")
    return all_approved_users


def check_file_coverage(changed_files, rules, approved_users):
    uncovered_files_list = []
    
    # Reverse rules to turn "Last Match Wins" into "First Match Wins"
    reversed_rules = list(reversed(rules))

    for file_path in changed_files:
        found_owners = None
        for rule in reversed_rules:
            for pattern in rule['patterns']:
                if file_path.match(pattern):
                    found_owners = rule['owners']
                    break 
            if found_owners:
                break
        
        if not found_owners:
            logging.warning(f"File {file_path} has no owner in CODEOWNERS-DWH. Failing.")
            uncovered_files_list.append(f"- {file_path} (has NO owner assigned in CODEOWNERS-DWH)")
            continue
        
        intersection = approved_users.intersection(found_owners)
        if not intersection:
            uncovered_files_list.append(f"- {file_path} (requires: {', '.join(found_owners)})")
        else:
            logging.info(f"PR for {file_path} is covered by approval from: {', '.join(intersection)}")
    
    return uncovered_files_list


def main():
    try:
        REQUIRED_ENV_VARS = {
            "GITHUB_TOKEN": TOKEN,
            "GITHUB_EVENT_PATH": GITHUB_EVENT_PATH,
            "GITHUB_REPOSITORY": GITHUB_REPOSITORY,
        }
        for var_name, value in REQUIRED_ENV_VARS.items():
            if not value:
                fail(f"{var_name} is not set.")

        owner, repo, pr_number = get_pr_context(GITHUB_REPOSITORY, GITHUB_EVENT_PATH)
        rules = parse_codeowners(CODEOWNERS_FILE)
        changed_files = get_changed_files(owner, repo, pr_number)
        approved_users = get_approved_users(owner, repo, pr_number)
        uncovered_files = check_file_coverage(changed_files, rules, approved_users)
        
        if uncovered_files:
            uncovered_files_str = '\n'.join(uncovered_files)
            error_message = (
                f"Not all files are covered by approval. "
                f"Approver(s) {', '.join(approved_users)} do not have permissions for:\n"
                f"{uncovered_files_str}"
            )
            fail(error_message)
        else:
            logging.info("All changed files are covered by owners.")
    
    except Exception as e:
        fail(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    main()
