"""
Shared config, auth and label-path logic for the Gmail "Old" label tidy-up.

Two separate OAuth scopes, deliberately:

  audit.py       gmail.readonly  - can read mail, cannot change a single label
  apply_moves.py gmail.labels    - can create/rename labels, cannot read a single email

They keep separate token files, so the audit physically cannot mutate anything
and the apply step physically cannot read your mail.
"""

import os
import random
import sys
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ===================== CONFIG =====================

# Top-level parents to process. Plain "starts with" match on the label name,
# so the emoji and their invisible variation selectors never have to be typed.
# Set to [] to process every top-level label.
SCOPE_PREFIXES = ['Dosh', 'Politics', 'Business', 'Computing', 'Journeys', 'Friends']

# A label is stale if it has no email newer than this many years.
STALE_YEARS = 2

# Rows within this many days either side of the stale line are flagged
# BORDERLINE and defaulted to SKIP - they are judgement calls, not automation
# calls. Flip one to MOVE in the CSV to include it.
BORDERLINE_DAYS = 183

# Name used when a new archive sub-label has to be created.
OLD_NAME = 'Old'

# A label with any of these as a path segment is already archived and is left
# entirely alone. Existing ones are reused as move targets, never duplicated.
ARCHIVE_SEGMENTS = ('old', 'zold')

# Cached last-email dates go stale as new mail arrives. Past this age the cache
# is discarded and every label re-read, so a run months later can never judge
# labels on dates from the previous run.
CACHE_MAX_AGE_DAYS = 7

# ==================================================

HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(HERE, 'credentials.json')
AUDIT_CSV = os.path.join(HERE, 'label_audit.csv')
CACHE_FILE = os.path.join(HERE, '.audit_cache.json')

SCOPE_READONLY = 'https://www.googleapis.com/auth/gmail.readonly'
SCOPE_LABELS = 'https://www.googleapis.com/auth/gmail.labels'

CSV_COLUMNS = [
    'LABEL', 'LABEL_ID', 'LAST_EMAIL', 'AGE_DAYS', 'MESSAGES',
    'LEAF', 'VERDICT', 'PROPOSED_NEW_NAME', 'ACTION', 'NOTE',
]


# ------------------------- auth -------------------------

def service(scope, token_filename):
    """Build a Gmail API client for one scope, reusing a stored token."""
    token_path = os.path.join(HERE, token_filename)
    creds = None

    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, [scope])
        except ValueError:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                sys.exit(
                    "No credentials.json in %s\n"
                    "Follow SETUP-LOCAL.md to create an OAuth client and download it here."
                    % HERE
                )
            os.chmod(CREDENTIALS_FILE, 0o600)   # downloads land world-readable
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, [scope])
            print("Opening a browser to authorise scope:\n  %s" % scope)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as fh:
            fh.write(creds.to_json())
        os.chmod(token_path, 0o600)

    return build('gmail', 'v1', credentials=creds, cache_discovery=False)


def retry(call, tries=6):
    """Run a Gmail API call, backing off on rate limits and transient errors."""
    for attempt in range(tries):
        try:
            return call()
        except HttpError as err:
            transient = err.resp.status in (403, 429, 500, 502, 503, 504)
            if transient and attempt < tries - 1:
                time.sleep((2 ** attempt) * 0.5 + random.random())
                continue
            raise


def list_user_labels(svc):
    """Every user-created label, as [{'id': ..., 'name': ...}]."""
    res = retry(lambda: svc.users().labels().list(userId='me').execute())
    return [
        {'id': l['id'], 'name': l['name']}
        for l in res.get('labels', [])
        if l.get('type') == 'user'
    ]


# ------------------------- label path logic -------------------------

def in_scope(name):
    if not SCOPE_PREFIXES:
        return True
    return any(name.startswith(prefix) for prefix in SCOPE_PREFIXES)


def is_archived(name):
    """True if any path segment marks this label as already archived."""
    return any(seg.strip().lower() in ARCHIVE_SEGMENTS for seg in name.split('/'))


def is_leaf(name, all_names):
    """A label is a leaf if no other label is nested beneath it.

    Gmail stores each label's full path as its name, so renaming a label that
    has children orphans every one of them. Only leaves are ever moved.
    """
    prefix = name + '/'
    return not any(other.startswith(prefix) for other in all_names)


def archive_container(name, all_names):
    """The container a stale label should move into.

    Reuses an existing Old or zOld at the right level rather than creating a
    second one. Returns (container_path, already_exists).
    """
    parent = '/'.join(name.split('/')[:-1])
    for candidate in (OLD_NAME, 'zOld'):
        path = '%s/%s' % (parent, candidate) if parent else candidate
        if path in all_names:
            return path, True
    path = '%s/%s' % (parent, OLD_NAME) if parent else OLD_NAME
    return path, False


def propose_name(name, all_names):
    """Insert the archive segment immediately before the final segment.

        Dosh 💹/Banks/Acme Bank  ->  Dosh 💹/Banks/Old/Acme Bank
        Hobbies 🎲/Chess        ->  Hobbies 🎲/Old/Chess
    """
    leaf = name.split('/')[-1]
    container, _ = archive_container(name, all_names)
    return '%s/%s' % (container, leaf)


def is_archive_container(name):
    """True if this label IS an Old/zOld folder, rather than something filed in one.

    Containers are structure. They are never moved in either direction.
    """
    return name.split('/')[-1].strip().lower() in ARCHIVE_SEGMENTS


def archive_segment_index(name):
    """Position of the archive segment nearest the leaf, or None if there is none.

    Only segments before the final one count - a trailing Old is the container
    itself, which is_archive_container handles.
    """
    parts = name.split('/')
    for index in range(len(parts) - 2, -1, -1):
        if parts[index].strip().lower() in ARCHIVE_SEGMENTS:
            return index
    return None


def revive_name(name):
    """Drop the archive segment nearest the leaf - the inverse of propose_name.

        Dosh 💹/Banks/Old/Acme Bank       ->  Dosh 💹/Banks/Acme Bank
        Computing 👾/Old/Vendor/Reseller  ->  Computing 👾/Vendor/Reseller

    Used when mail starts arriving on a label that was previously archived.
    Returns None if there is no archive segment to drop.
    """
    index = archive_segment_index(name)
    if index is None:
        return None
    parts = name.split('/')
    del parts[index]
    return '/'.join(parts)
