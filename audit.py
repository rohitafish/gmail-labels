#!/usr/bin/env python3
"""
PHASE 1 - read only. Changes nothing in Gmail.

Scans every in-scope label, records the date of the most recent email on each,
and proposes a new name for the stale ones. Writes label_audit.csv for review.

    python3 audit.py                  # full audit, uses cache where possible
    python3 audit.py --refresh        # ignore the cache, re-read every label
    python3 audit.py --only Journeys  # restrict to one parent (for verification)

Results are cached in .audit_cache.json so re-runs are instant. The cache is
keyed by label id, so renaming a label in Gmail invalidates nothing you need.
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import gmail_common as gc


def last_message(svc, label_id):
    """(datetime_or_None, estimated_message_count) for the newest email on a label.

    Gmail returns messages newest-first, so one result is enough. Working at
    message level rather than thread level matters: a thread's last message may
    not itself carry the label, which would report a date that is too recent.
    """
    res = gc.retry(lambda: svc.users().messages().list(
        userId='me', labelIds=[label_id], maxResults=1).execute())

    messages = res.get('messages', [])
    estimate = res.get('resultSizeEstimate', 0)
    if not messages:
        return None, 0

    msg = gc.retry(lambda: svc.users().messages().get(
        userId='me', id=messages[0]['id'], format='minimal').execute())
    when = datetime.fromtimestamp(int(msg['internalDate']) / 1000.0, tz=timezone.utc)
    return when, estimate


def load_cache(refresh):
    """Cached dates from a recent run, or {} if absent, stale or unreadable.

    Anything older than CACHE_MAX_AGE_DAYS is thrown away. Without that, a run
    six months from now would happily reuse today's dates and call a label
    stale on the strength of mail it never re-read.
    """
    if refresh or not os.path.exists(gc.CACHE_FILE):
        return {}
    try:
        with open(gc.CACHE_FILE) as fh:
            blob = json.load(fh)
    except (ValueError, OSError):
        return {}

    if not isinstance(blob, dict) or blob.get('version') != 1:
        return {}                       # older format - discard rather than trust

    try:
        written = datetime.fromisoformat(blob['written'])
    except (KeyError, TypeError, ValueError):
        return {}

    age_days = (datetime.now(timezone.utc) - written).days
    if age_days > gc.CACHE_MAX_AGE_DAYS:
        print('Cache is %d days old (limit %d) - re-reading every label.'
              % (age_days, gc.CACHE_MAX_AGE_DAYS))
        return {}

    print('Reusing cached dates from %s (%d days old).'
          % (written.date(), age_days))
    return blob.get('entries', {})


def save_cache(entries):
    with open(gc.CACHE_FILE, 'w') as fh:
        json.dump({
            'version': 1,
            'written': datetime.now(timezone.utc).isoformat(),
            'entries': entries,
        }, fh)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--refresh', action='store_true',
                        help='ignore the cache and re-read every label')
    parser.add_argument('--only', metavar='PREFIX',
                        help='restrict to one top-level parent, e.g. Journeys')
    parser.add_argument('--out', default=gc.AUDIT_CSV, help='output CSV path')
    args = parser.parse_args()

    svc = gc.service(gc.SCOPE_READONLY, 'token_readonly.json')

    labels = gc.list_user_labels(svc)
    all_names = {l['name'] for l in labels}
    print('%d user labels in the account.' % len(labels))

    targets = [
        l for l in labels
        if gc.in_scope(l['name'])
        and not gc.is_archived(l['name'])
        and (args.only is None or l['name'].startswith(args.only))
    ]
    targets.sort(key=lambda l: l['name'])

    skipped_archived = sum(
        1 for l in labels
        if gc.in_scope(l['name']) and gc.is_archived(l['name'])
        and (args.only is None or l['name'].startswith(args.only))
    )
    print('%d labels in scope to examine (%d already archived, left alone).'
          % (len(targets), skipped_archived))

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=365.2425 * gc.STALE_YEARS)
    stale_days = (now - cutoff).days
    print('Stale cutoff: %s (%d days ago). Borderline band: +/-%d days.\n'
          % (cutoff.date(), stale_days, gc.BORDERLINE_DAYS))

    cache = load_cache(args.refresh)
    rows = []
    counts = defaultdict(int)
    lookups = 0

    for index, label in enumerate(targets, start=1):
        name = label['name']
        label_id = label['id']

        if label_id in cache:
            entry = cache[label_id]
        else:
            when, estimate = last_message(svc, label_id)
            entry = {'last': when.isoformat() if when else None, 'messages': estimate}
            cache[label_id] = entry
            lookups += 1
            time.sleep(0.05)
            if lookups % 25 == 0:
                save_cache(cache)
                print('  ...%d/%d examined' % (index, len(targets)))

        leaf = gc.is_leaf(name, all_names)
        last_iso = entry['last']
        messages = entry['messages']

        proposed = ''
        note = ''

        if last_iso is None:
            action = 'SKIP'
            age_days = ''
            last_display = ''
            if leaf:
                verdict = 'EMPTY'
                note = 'No emails at all - candidate for deletion, not archiving.'
            else:
                # A parent carrying no mail of its own is structure, not clutter.
                # Deleting it would orphan everything beneath it.
                verdict = 'EMPTY (has sub-labels)'
                note = ('No mail of its own, but it has sub-labels - this is a '
                        'container. Do not delete it.')
        else:
            when = datetime.fromisoformat(last_iso)
            age_days = (now - when).days
            last_display = when.date().isoformat()
            stale = when < cutoff
            borderline = abs(age_days - stale_days) <= gc.BORDERLINE_DAYS

            if not leaf:
                verdict = 'STALE (has sub-labels)' if stale else 'ACTIVE (has sub-labels)'
                action = 'SKIP'
                note = 'Has sub-labels - never moved; its children are handled individually.'
            else:
                verdict = 'STALE' if stale else 'ACTIVE'
                if stale or borderline:
                    proposed = gc.propose_name(name, all_names)

                if proposed and proposed in all_names:
                    action = 'SKIP'
                    note = 'A label named "%s" already exists.' % proposed
                    counts['collision'] += 1
                elif borderline:
                    action = 'SKIP'
                    note = ('BORDERLINE - %d days, within %d of the %d-day line. '
                            'Set ACTION to MOVE to include it.'
                            % (age_days, gc.BORDERLINE_DAYS, stale_days))
                    counts['borderline'] += 1
                elif stale:
                    action = 'MOVE'
                else:
                    action = 'SKIP'

        counts[verdict] += 1
        if action == 'MOVE':
            counts['queued'] += 1

        rows.append({
            'LABEL': name,
            'LABEL_ID': label_id,
            'LAST_EMAIL': last_display,
            'AGE_DAYS': age_days,
            'MESSAGES': messages,
            'LEAF': 'yes' if leaf else 'no',
            'VERDICT': verdict,
            'PROPOSED_NEW_NAME': proposed,
            'ACTION': action,
            'NOTE': note,
        })

    save_cache(cache)

    rows.sort(key=lambda r: (r['AGE_DAYS'] == '', -(r['AGE_DAYS'] or 0)))

    # utf-8-sig so Excel opens the emoji label names correctly.
    with open(args.out, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=gc.CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print('\n--- SUMMARY ---')
    print('Labels examined          %d  (%d fresh lookups, rest cached)'
          % (len(rows), lookups))
    for verdict in ('ACTIVE', 'STALE', 'EMPTY',
                    'ACTIVE (has sub-labels)', 'STALE (has sub-labels)'):
        if counts[verdict]:
            print('%-24s %d' % (verdict, counts[verdict]))
    print('Borderline (defaulted to SKIP) %d' % counts['borderline'])
    if counts['collision']:
        print('Name collisions (skipped)      %d' % counts['collision'])
    print('QUEUED AS MOVE           %d' % counts['queued'])

    report_fully_stale_branches(rows)

    print('\nWritten: %s' % args.out)
    print('Review the ACTION column, then run:  python3 apply_moves.py')


def report_fully_stale_branches(rows):
    """Categories where every leaf came out stale - candidates for moving the
    branch as a unit instead of filling it with an Old folder. Report only."""
    total = defaultdict(int)
    stale = defaultdict(int)

    for row in rows:
        if row['LEAF'] != 'yes':
            continue
        parent = '/'.join(row['LABEL'].split('/')[:-1])
        if not parent:
            continue
        total[parent] += 1
        if row['VERDICT'].startswith('STALE'):
            stale[parent] += 1

    fully = sorted(p for p in total if total[p] > 1 and stale[p] == total[p])
    if not fully:
        print('\nNo category came out entirely stale.')
        return

    print('\n--- CATEGORIES WHERE EVERY CHILD IS STALE (%d) ---' % len(fully))
    print('Nothing is collapsed automatically. Your call whether to move these as a unit.')
    for parent in fully:
        print('  %-55s %d of %d' % (parent, stale[parent], total[parent]))


if __name__ == '__main__':
    sys.exit(main())
