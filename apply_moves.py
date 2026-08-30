#!/usr/bin/env python3
"""
PHASE 2 - performs the renames. Dry run unless you pass --apply.

Reads label_audit.csv and acts only on rows whose ACTION cell says MOVE, so you
veto anything by changing it to SKIP first.

    python3 apply_moves.py            # dry run - prints every rename it would make
    python3 apply_moves.py --apply    # actually do it

Renaming a label preserves every email on it. Nothing here deletes mail, and
every rename is reversible by renaming back - the original name is kept in the
LABEL column of the CSV, and in apply_log.csv afterwards.

This runs on the gmail.labels scope, which cannot read a single email.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime

from googleapiclient.errors import HttpError

import gmail_common as gc


def create_label(svc, name):
    """Create a new user label named `name`, visible in the sidebar and the
    message list - the same defaults Gmail's own "create label" uses."""
    return gc.retry(lambda: svc.users().labels().create(
        userId='me',
        body={'name': name,
              'labelListVisibility': 'labelShow',
              'messageListVisibility': 'show'}).execute())


def rename_label(svc, label_id, new_name):
    """Rename an existing label in place. Preserves every email on it."""
    return gc.retry(lambda: svc.users().labels().patch(
        userId='me', id=label_id, body={'name': new_name}).execute())


def read_jobs(path):
    with open(path, encoding='utf-8-sig') as fh:
        rows = list(csv.DictReader(fh))

    jobs = []
    for row in rows:
        if row.get('ACTION', '').strip().upper() != 'MOVE':
            continue
        old_name = row.get('LABEL', '').strip()
        new_name = row.get('PROPOSED_NEW_NAME', '').strip()
        if not old_name or not new_name:
            print('SKIP  %s  (ACTION is MOVE but PROPOSED_NEW_NAME is empty)'
                  % (old_name or '<blank>'))
            continue
        jobs.append({
            'old': old_name,
            'new': new_name,
            'id': row.get('LABEL_ID', '').strip(),
        })

    # Deepest paths first, so a parent is never renamed out from under a child.
    jobs.sort(key=lambda j: len(j['old'].split('/')), reverse=True)
    return rows, jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true',
                        help='actually perform the renames (default is a dry run)')
    parser.add_argument('--csv', default=gc.AUDIT_CSV, help='audit CSV to read')
    args = parser.parse_args()
    dry = not args.apply

    # read_jobs also returns every parsed row (not just MOVE ones), kept for
    # callers that want to report on the full CSV; main() only needs jobs.
    _rows, jobs = read_jobs(args.csv)
    if not jobs:
        print('Nothing marked MOVE in %s - nothing to do.' % args.csv)
        return 0

    svc = gc.service(gc.SCOPE_LABELS, 'token_labels.json')

    labels = gc.list_user_labels(svc)
    id_by_name = {label['name']: label['id'] for label in labels}
    existing = set(id_by_name)

    tag = '[dry] ' if dry else ''
    print('%s%d move(s) queued from %s\n' % ('DRY RUN - ' if dry else '', len(jobs), args.csv))

    results = []
    moved = failed = created = 0

    for job in jobs:
        old, new = job['old'], job['new']

        label_id = job['id'] or id_by_name.get(old)
        if not label_id or old not in existing:
            print('SKIP    %s  (label no longer exists in Gmail)' % old)
            results.append((old, new, 'SKIPPED', 'label no longer exists'))
            failed += 1
            continue

        if new in existing:
            print('SKIP    %s  ->  %s  (target already exists)' % (old, new))
            results.append((old, new, 'SKIPPED', 'target already exists'))
            failed += 1
            continue

        # Make sure the intermediate archive container exists as a real label.
        container = '/'.join(new.split('/')[:-1])
        if container and container not in existing:
            print('%sCREATE  %s' % (tag, container))
            if not dry:
                try:
                    made = create_label(svc, container)
                    id_by_name[container] = made['id']
                except HttpError as err:
                    if err.resp.status != 409:      # 409 = already exists, fine
                        print('FAILED  creating %s : %s' % (container, err))
                        results.append((old, new, 'FAILED', 'container: %s' % err))
                        failed += 1
                        continue
            existing.add(container)
            created += 1

        print('%sMOVE    %s  ->  %s' % (tag, old, new))
        if dry:
            results.append((old, new, 'DRY-RUN', ''))
            continue

        try:
            rename_label(svc, label_id, new)
            existing.discard(old)
            existing.add(new)
            id_by_name[new] = label_id
            results.append((old, new, 'DONE', ''))
            moved += 1
            time.sleep(0.12)          # stay well inside Gmail API rate limits
        except HttpError as err:
            print('FAILED  %s : %s' % (old, err))
            results.append((old, new, 'FAILED', str(err)))
            failed += 1

    # Timestamped, and derived from the input we were actually given - never a
    # fixed name that could land on top of the CSV we just read, or on top of
    # an earlier run's log. Those logs are the only record of the old names.
    stamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
    log_path = os.path.join(os.path.dirname(os.path.abspath(args.csv)),
                            'apply_log-%s%s.csv' % (stamp, '-dryrun' if dry else ''))
    if os.path.abspath(log_path) == os.path.abspath(args.csv):
        sys.exit('Refusing to write the log over the input CSV: %s' % args.csv)

    with open(log_path, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.writer(fh)
        writer.writerow(['ORIGINAL_NAME', 'NEW_NAME', 'RESULT', 'ERROR'])
        writer.writerows(results)

    print()
    if dry:
        print('DRY RUN finished - nothing was changed.')
        print('%d rename(s) and %d new container(s) would happen.' % (len(jobs), created))
        print('Re-run with --apply when you are happy with the plan.')
    else:
        print('Done. %d moved, %d containers created, %d skipped/failed.'
              % (moved, created, failed))
        print('KEEP %s - it is the only record of the original names.' % log_path)
        print('Gmail\'s sidebar can take a minute to catch up - reload Gmail if it looks odd.')
    print('Log: %s' % log_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
