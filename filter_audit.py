#!/usr/bin/env python3
"""
Read only. Changes nothing in Gmail.

Works out which Gmail filters are safe to delete. It reads the filters from
the XML file Gmail exports (Settings -> Filters and Blocked Addresses -> select
all -> Export), runs each filter's criteria as a Gmail search, and writes
filter_audit.csv with a verdict and a suggested ACTION for each one.

    ./run filter_audit.py                       # audit filters/mailFilters.xml
    ./run filter_audit.py --limit 20            # quick trial on the first 20
    ./run filter_audit.py --refresh             # ignore cached search results
    ./run filter_audit.py --prune filter_audit.csv
        # write filters/mailFilters-pruned.xml without the ACTION=DELETE rows

Uses the same gmail.readonly token as audit.py. Search results are cached in
.filter_audit_cache.json for CACHE_MAX_AGE_DAYS, keyed by the search itself,
so filters with identical criteria cost one search between them.

Pruning never touches Gmail either. To apply it, keep the original export as
the backup, delete every filter in Gmail, then Import the pruned file.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import defusedxml.ElementTree as SafeET

import gmail_common as gc

# ===================== CONFIG =====================

# A filter whose newest matching email is older than this is DEAD.
DEAD_YEARS = 2

# Newest match between QUIET_YEARS and DEAD_YEARS old: QUIET, left for review.
QUIET_YEARS = 1

# Recent matches are counted up to this many; beyond it the CSV says "500+".
COUNT_CAP = 500

# Actions where deleting a quiet filter has a cost the match count can't show,
# so a dead one is suggested for REVIEW rather than DELETE:
#   forwardTo                    mail silently stops going somewhere else
#   shouldNeverSpam              protects rare senders from the spam folder
#   shouldTrash                  trashed mail is purged after 30 days, so
#                                a working delete filter looks unused
#   shouldAlwaysMarkAsImportant  usually there for a reason
CAREFUL_ACTIONS = ('forwardTo', 'shouldNeverSpam', 'shouldTrash',
                   'shouldAlwaysMarkAsImportant')

# ==================================================

DEFAULT_XML = os.path.join(gc.HERE, 'filters', 'mailFilters.xml')
DEFAULT_CSV = os.path.join(gc.HERE, 'filter_audit.csv')
DEFAULT_PRUNED = os.path.join(gc.HERE, 'filters', 'mailFilters-pruned.xml')
FILTER_CACHE_FILE = os.path.join(gc.HERE, '.filter_audit_cache.json')

ATOM = 'http://www.w3.org/2005/Atom'
APPS = 'http://schemas.google.com/apps/2006'
NS = {'a': ATOM, 'apps': APPS}

CRITERIA_KEYS = ('from', 'to', 'subject', 'hasTheWord', 'doesNotHaveTheWord',
                 'hasAttachment', 'size')
ACTION_KEYS = ('label', 'smartLabelToApply', 'forwardTo', 'shouldArchive',
               'shouldMarkAsRead', 'shouldStar', 'shouldTrash',
               'shouldNeverSpam', 'shouldAlwaysMarkAsImportant',
               'shouldNeverMarkAsImportant')
# Written by every export whether or not a size criterion is set.
IGNORED_KEYS = ('sizeOperator', 'sizeUnit', 'excludeChats')

SIZE_UNITS = {'s_sb': '', 's_skb': 'K', 's_smb': 'M'}

CSV_COLUMNS = ['N', 'FILTER_ID', 'CRITERIA', 'LABEL', 'ACTIONS', 'LAST_MATCH',
               'AGE_DAYS', 'RECENT_MATCHES', 'VERDICT', 'ACTION', 'NOTE']


# ------------------------- reading the export -------------------------

def load_filters(path):
    """[{'n', 'id', 'props'}] in export order, read with defusedxml."""
    root = SafeET.parse(path).getroot()
    filters = []
    for n, entry in enumerate(root.findall('a:entry', NS), start=1):
        raw_id = entry.findtext('a:id', default='', namespaces=NS)
        props = {p.get('name'): p.get('value', '')
                 for p in entry.findall('apps:property', NS)}
        filters.append({'n': n, 'id': raw_id.rsplit(':', 1)[-1], 'props': props})
    return filters


def build_query(props):
    """The Gmail search that matches what this filter matches, or '' if the
    filter has no criteria at all."""
    parts = []
    for key, operator in (('from', 'from'), ('to', 'to'), ('subject', 'subject')):
        if props.get(key):
            parts.append('%s:(%s)' % (operator, props[key]))
    if props.get('hasTheWord'):
        parts.append('(%s)' % props['hasTheWord'])
    if props.get('doesNotHaveTheWord'):
        parts.append('-{%s}' % props['doesNotHaveTheWord'])
    if props.get('hasAttachment') == 'true':
        parts.append('has:attachment')
    if props.get('size'):
        operator = 'smaller' if props.get('sizeOperator') == 's_ss' else 'larger'
        unit = SIZE_UNITS.get(props.get('sizeUnit'), '')
        parts.append('%s:%s%s' % (operator, props['size'], unit))
    return ' '.join(parts)


def describe_actions(props):
    """Every action except the label, as short words for the CSV."""
    words = []
    for key in ACTION_KEYS:
        if key == 'label' or key not in props:
            continue
        if key == 'forwardTo':
            words.append('forward')
        elif key == 'smartLabelToApply':
            words.append('category')
        else:
            words.append(re.sub(r'^should', '', key))
    return ' '.join(words)


def _norm(value):
    return ' '.join(value.lower().split())


def criteria_key(props):
    return tuple((k, _norm(props[k])) for k in CRITERIA_KEYS if props.get(k))


def actions_key(props):
    return tuple((k, _norm(props[k])) for k in ACTION_KEYS if k in props)


def _single_token(props):
    """The one bare word a filter matches on (an address or a domain), or
    None if its criteria are anything more complicated."""
    key = criteria_key(props)
    if len(key) != 1 or key[0][0] not in ('from', 'hasTheWord'):
        return None
    value = key[0][1]
    if re.fullmatch(r'[a-z0-9._%+\-@]+', value):
        return key[0][0], value
    return None


def find_overlaps(filters):
    """{filter_n: note} for filters another filter already does the job of.

    DUPLICATE: same criteria and same actions as an earlier filter.
    REDUNDANT: same actions, and a broader filter already matches all its
    mail -- e.g. `news@shop.example.com` next to `shop.example.com`.
    """
    verdicts = {}
    seen = {}
    for f in filters:
        key = (criteria_key(f['props']), actions_key(f['props']))
        if key[0] and key in seen:
            verdicts[f['n']] = ('DUPLICATE', 'Same criteria and actions as filter %d.'
                                % seen[key])
        else:
            seen.setdefault(key, f['n'])

    by_actions = defaultdict(list)
    for f in filters:
        token = _single_token(f['props'])
        if token and f['n'] not in verdicts:
            by_actions[(actions_key(f['props']), token[0])].append((token[1], f['n']))

    for group in by_actions.values():
        tokens = dict(group)
        for value, n in group:
            # Walk up: news@mail.shop.example.com -> mail.shop.example.com -> shop.example.com
            rest = value.split('@', 1)[1] if '@' in value else value
            candidates = [rest] if '@' in value else []
            while '.' in rest:
                rest = rest.split('.', 1)[1]
                if '.' in rest:
                    candidates.append(rest)
            for broader in candidates:
                if broader != value and broader in tokens:
                    verdicts[n] = ('REDUNDANT', 'Filter %d (%s) already matches '
                                   'this mail with the same actions.'
                                   % (tokens[broader], broader))
                    break
    return verdicts


# ------------------------- classification -------------------------

def classify(props, overlap, match, label_names, now):
    """(VERDICT, ACTION, NOTE) for one filter. Pure -- no service, no clock.

    `overlap` is find_overlaps' (verdict, note) or None. `match` is
    {'last': iso_or_None, 'recent': int_or_str} from the search, or None if
    no search was run.
    """
    label = props.get('label')
    has_action = any(k in props for k in ACTION_KEYS)
    careful = [k for k in CAREFUL_ACTIONS if k in props]
    notes = []

    if not build_query(props):
        return 'NO CRITERIA', 'REVIEW', 'Matches nothing that can be searched for.'
    if not has_action:
        return 'NO ACTION', 'DELETE', 'Does nothing to the mail it matches.'
    if overlap:
        return overlap[0], 'DELETE', overlap[1]

    label_missing = label is not None and label not in label_names
    if label_missing:
        notes.append('Its label "%s" no longer exists.' % label)

    last = datetime.fromisoformat(match['last']) if match and match['last'] else None
    if last is None:
        verdict = 'NEVER MATCHED'
    elif last < now - timedelta(days=365.2425 * DEAD_YEARS):
        verdict = 'DEAD'
    elif last < now - timedelta(days=365.2425 * QUIET_YEARS):
        verdict = 'QUIET'
    else:
        verdict = 'ACTIVE'

    if verdict in ('NEVER MATCHED', 'DEAD'):
        if careful:
            action = 'REVIEW'
            notes.append('No recent mail, but it does %s - check before deleting.'
                         % ', '.join(careful))
            if 'shouldTrash' in careful:
                notes.append('Trash empties after 30 days, so a working delete '
                             'filter can look unused.')
        else:
            action = 'DELETE'
    elif verdict == 'QUIET':
        action = 'REVIEW'
        notes.append('Last match %d-%d years ago.' % (QUIET_YEARS, DEAD_YEARS))
    elif label_missing:
        action = 'REVIEW'
        notes.append('Still matching mail - re-point it at a label that exists.')
    else:
        action = 'KEEP'

    return verdict, action, ' '.join(notes)


# ------------------------- searching -------------------------

def search(svc, query, since):
    """{'last': iso_or_None, 'recent': int_or_'N+'} for one Gmail search,
    across every folder including Spam and Trash."""
    res = gc.retry(lambda: svc.users().messages().list(
        userId='me', q=query, maxResults=1, includeSpamTrash=True).execute())
    messages = res.get('messages', [])
    if not messages:
        return {'last': None, 'recent': 0}

    msg = gc.retry(lambda: svc.users().messages().get(
        userId='me', id=messages[0]['id'], format='minimal').execute())
    last = datetime.fromtimestamp(int(msg['internalDate']) / 1000.0, tz=UTC)
    if last < since:
        return {'last': last.isoformat(), 'recent': 0}

    recent_q = '%s after:%s' % (query, since.strftime('%Y/%m/%d'))
    res = gc.retry(lambda: svc.users().messages().list(
        userId='me', q=recent_q, maxResults=COUNT_CAP, includeSpamTrash=True).execute())
    count = len(res.get('messages', []))
    recent = '%d+' % count if res.get('nextPageToken') else count
    return {'last': last.isoformat(), 'recent': recent}


def load_cache(path, refresh, now):
    """Cached search results younger than gc.CACHE_MAX_AGE_DAYS, or {}."""
    if refresh or not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            blob = json.load(fh)
    except (ValueError, OSError):
        return {}
    if not isinstance(blob, dict) or blob.get('version') != 1:
        return {}
    valid = {}
    for query, entry in blob.get('entries', {}).items():
        if not isinstance(entry, dict) or not {'last', 'recent'} <= entry.keys():
            continue
        try:
            fetched = datetime.fromisoformat(entry.get('fetched'))
        except (TypeError, ValueError):
            continue
        if fetched.tzinfo and (now - fetched).days <= gc.CACHE_MAX_AGE_DAYS:
            valid[query] = entry
    return valid


def save_cache(path, entries):
    """Atomic, owner-only: the cache holds search terms from your filters."""
    tmp = path + '.tmp'
    gc._write_private(tmp, json.dumps({'version': 1, 'entries': entries}))
    os.replace(tmp, path)


# ------------------------- pruning -------------------------

def prune(xml_path, csv_path, out_path):
    """Write a copy of the export without the filters the CSV marks DELETE.
    Returns (kept, dropped)."""
    with open(csv_path, newline='', encoding='utf-8-sig') as fh:
        rows = list(csv.DictReader(fh))
    if not rows or 'FILTER_ID' not in rows[0] or 'ACTION' not in rows[0]:
        sys.exit('%s has no FILTER_ID/ACTION columns - is it a filter_audit.csv?'
                 % csv_path)
    actions = {r['FILTER_ID'].lstrip("'"): r['ACTION'].strip().upper() for r in rows}

    ET.register_namespace('', ATOM)
    ET.register_namespace('apps', APPS)
    tree = SafeET.parse(xml_path)
    root = tree.getroot()
    entries = root.findall('a:entry', NS)

    xml_ids = {e.findtext('a:id', default='', namespaces=NS).rsplit(':', 1)[-1]
               for e in entries}
    unknown = sorted(set(actions) - xml_ids)
    if unknown:
        sys.exit('The CSV names %d filter(s) that are not in %s (e.g. %s). '
                 'Was it made from a different export?'
                 % (len(unknown), xml_path, unknown[0]))

    kept_ids, dropped = [], 0
    for entry in entries:
        fid = entry.findtext('a:id', default='', namespaces=NS).rsplit(':', 1)[-1]
        if actions.get(fid) == 'DELETE':
            root.remove(entry)
            dropped += 1
        else:
            kept_ids.append(fid)

    feed_id = root.find('a:id', NS)
    if feed_id is not None:
        feed_id.text = 'tag:mail.google.com,2008:filters:' + ','.join(kept_ids)
    tree.write(out_path, encoding='UTF-8', xml_declaration=True)
    return len(kept_ids), dropped


# ------------------------- main -------------------------

def audit(args):
    filters = load_filters(args.xml)
    print('%d filters in %s.' % (len(filters), args.xml))
    if args.limit:
        filters = filters[:args.limit]
        print('Auditing the first %d only (--limit).' % len(filters))

    svc = gc.service(gc.SCOPE_READONLY, 'token_readonly.json')
    label_names = {label['name'] for label in gc.list_user_labels(svc)}

    now = datetime.now(UTC)
    since = now - timedelta(days=365.2425 * QUIET_YEARS)
    overlaps = find_overlaps(filters)
    cache = load_cache(args.cache, args.refresh, now)
    if cache:
        print('Reusing %d cached search result(s).' % len(cache))

    rows, counts, searches = [], defaultdict(int), 0
    for index, f in enumerate(filters, start=1):
        props = f['props']
        query = build_query(props)
        overlap = overlaps.get(f['n'])
        match = None
        if query and not overlap and any(k in props for k in ACTION_KEYS):
            if query not in cache:
                cache[query] = dict(search(svc, query, since),
                                    fetched=datetime.now(UTC).isoformat())
                searches += 1
                time.sleep(0.05)
                if searches % 25 == 0:
                    save_cache(args.cache, cache)
                    print('  ...%d/%d filters checked' % (index, len(filters)))
            match = cache[query]

        verdict, action, note = classify(props, overlap, match, label_names, now)
        counts[verdict] += 1
        counts['action:' + action] += 1
        last = datetime.fromisoformat(match['last']) if match and match['last'] else None
        rows.append({
            'N': f['n'],
            'FILTER_ID': f['id'],
            'CRITERIA': query,
            'LABEL': props.get('label', ''),
            'ACTIONS': describe_actions(props),
            'LAST_MATCH': last.date().isoformat() if last else '',
            'AGE_DAYS': (now - last).days if last else '',
            'RECENT_MATCHES': match['recent'] if match else '',
            'VERDICT': verdict,
            'ACTION': action,
            'NOTE': note,
        })
    save_cache(args.cache, cache)

    order = {'DELETE': 0, 'REVIEW': 1, 'KEEP': 2}
    rows.sort(key=lambda r: (order[r['ACTION']], r['VERDICT'], r['N']))
    with open(args.out, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows({k: gc.csv_safe(v) for k, v in row.items()} for row in rows)

    print('\n--- SUMMARY ---')
    print('Filters audited  %d  (%d fresh searches, rest cached or not needed)'
          % (len(rows), searches))
    for verdict in ('ACTIVE', 'QUIET', 'DEAD', 'NEVER MATCHED', 'DUPLICATE',
                    'REDUNDANT', 'NO ACTION', 'NO CRITERIA'):
        if counts[verdict]:
            print('  %-15s %d' % (verdict, counts[verdict]))
    print('Suggested: DELETE %d, REVIEW %d, KEEP %d'
          % (counts['action:DELETE'], counts['action:REVIEW'], counts['action:KEEP']))
    report_shared_labels(filters)
    print('\nWritten: %s' % args.out)
    print('Edit the ACTION column, then:  ./run filter_audit.py --prune %s'
          % os.path.basename(args.out))


def report_shared_labels(filters):
    """Labels fed by several filters with the same actions: candidates for
    merging into one filter. Report only."""
    groups = defaultdict(int)
    for f in filters:
        if 'label' in f['props']:
            groups[actions_key(f['props'])] += 1
    mergeable = sorted((n for n in groups.values() if n > 1), reverse=True)
    if mergeable:
        print('\n%d labels are fed by 2+ filters with identical actions (%d filters '
              'in all, largest group %d). Merging each group into one filter '
              'would shorten the list further.'
              % (len(mergeable), sum(mergeable), mergeable[0]))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--xml', default=DEFAULT_XML, help='Gmail filter export')
    parser.add_argument('--out', default=DEFAULT_CSV, help='output CSV path')
    parser.add_argument('--cache', default=FILTER_CACHE_FILE, help=argparse.SUPPRESS)
    parser.add_argument('--refresh', action='store_true',
                        help='ignore cached search results')
    parser.add_argument('--limit', type=int, metavar='N',
                        help='audit only the first N filters (a trial run)')
    parser.add_argument('--prune', metavar='CSV',
                        help='write a pruned export from an edited audit CSV')
    parser.add_argument('--pruned-out', default=DEFAULT_PRUNED,
                        help='where --prune writes (default filters/mailFilters-pruned.xml)')
    args = parser.parse_args()

    if args.prune:
        kept, dropped = prune(args.xml, args.prune, args.pruned_out)
        print('Kept %d filters, dropped %d.\nWritten: %s' % (kept, dropped, args.pruned_out))
        print('Gmail import ADDS filters, it does not replace them. Keep %s as the '
              'backup, delete all filters in Gmail, then Import the pruned file.'
              % os.path.basename(args.xml))
        return
    audit(args)


if __name__ == '__main__':
    sys.exit(main())
