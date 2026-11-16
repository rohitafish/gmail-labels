#!/usr/bin/env python3
"""
PHASE 2 - performs the renames. Dry run unless you pass --apply.

Reads label_audit.csv and acts only on rows whose ACTION cell says MOVE, so you
veto anything by changing it to SKIP first.

    python3 apply_moves.py            # dry run - prints every rename it would make
    python3 apply_moves.py --apply    # actually do it

An audit CSV more than CACHE_MAX_AGE_DAYS old is refused by --apply unless you
also pass --allow-old-csv: this step cannot read mail, so it cannot tell
whether a label has had new mail since the audit. Re-run audit.py instead.

Renaming a label preserves every email on it. Nothing here deletes mail, and
every rename is reversible by renaming back - the original name is kept in the
LABEL column of the CSV, and in the timestamped apply_log-*.csv, which is
written row by row as the run goes.

This runs on the gmail.labels scope, which cannot read a single email.
"""

import argparse
import csv
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta

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


def audit_age_days(path, rows, today=None):
    """Whole days since the audit that produced this CSV.

    Taken from the data where possible: LAST_EMAIL plus AGE_DAYS is the day
    the audit ran (give or take one), and unlike the file's modification time
    it doesn't move when the ACTION column is edited weeks later. Falls back
    to the modification time when no row carries both (every label empty, or
    a spreadsheet re-saved the dates in another format).
    """
    today = today or datetime.now(UTC).date()
    for row in rows:
        try:
            audited = (date.fromisoformat((row.get('LAST_EMAIL') or '').strip())
                       + timedelta(days=int(row.get('AGE_DAYS'))))
        except (TypeError, ValueError, OverflowError):
            continue
        return (today - audited).days
    modified = datetime.fromtimestamp(os.path.getmtime(path), tz=UTC).date()
    return (today - modified).days


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true',
                        help='actually perform the renames (default is a dry run)')
    parser.add_argument('--csv', default=gc.AUDIT_CSV, help='audit CSV to read')
    parser.add_argument('--allow-old-csv', action='store_true',
                        help='with --apply, act on an audit CSV older than '
                             'CACHE_MAX_AGE_DAYS instead of refusing')
    args = parser.parse_args()
    dry = not args.apply

    rows, jobs = read_jobs(args.csv)
    if not jobs:
        print('Nothing marked MOVE in %s - nothing to do.' % args.csv)
        return 0

    # This step runs on a scope that cannot read mail, so it has no way to
    # notice that a label queued for archiving has received mail since the
    # audit. The only defence is not acting on an old audit.
    age = audit_age_days(args.csv, rows)
    old_csv = age > gc.CACHE_MAX_AGE_DAYS
    if old_csv:
        warning = ('%s is from an audit %d days ago (limit %d). Labels may have '
                   'received mail since - re-run audit.py first.'
                   % (args.csv, age, gc.CACHE_MAX_AGE_DAYS))
        if not dry and not args.allow_old_csv:
            sys.exit(warning + '\nNothing was changed. To apply it anyway, add '
                               '--allow-old-csv.')
        print('WARNING: %s\n' % warning)

    # Timestamped, and derived from the input we were actually given - never a
    # fixed name that could land on top of the CSV we just read, or on top of
    # an earlier run's log. Those logs are the only record of the old names.
    # Settled before anything is renamed, so a bad path stops the run while
    # there is still nothing to record.
    stamp = time.strftime('%Y-%m-%d-%H%M%S')
    log_path = os.path.join(os.path.dirname(os.path.abspath(args.csv)),
                            'apply_log-%s%s.csv' % (stamp, '-dryrun' if dry else ''))
    if os.path.abspath(log_path) == os.path.abspath(args.csv):
        sys.exit('Refusing to write the log over the input CSV: %s' % args.csv)

    svc = gc.service(gc.SCOPE_LABELS, 'token_labels.json')

    labels = gc.list_user_labels(svc)
    id_by_name = {label['name']: label['id'] for label in labels}
    existing = set(id_by_name)

    tag = '[dry] ' if dry else ''
    print('%s%d move(s) queued from %s\n' % ('DRY RUN - ' if dry else '', len(jobs), args.csv))

    moved = failed = created = planned = 0

    # The log is written and flushed one row at a time, not collected and
    # written at the end: a dropped connection or Ctrl-C part-way through
    # would otherwise leave labels renamed and no log of what they were.
    with open(log_path, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.writer(fh)

        def record(*fields):
            # Every field passed through csv_safe() -- this log gets opened
            # in a spreadsheet during the undo procedure (SETUP-LOCAL.md),
            # and a label name starting with =/+/-/@ would otherwise be read
            # as a formula.
            writer.writerow([gc.csv_safe(field) for field in fields])
            fh.flush()

        record('ORIGINAL_NAME', 'NEW_NAME', 'RESULT', 'ERROR')
        try:
            for job in jobs:
                old, new = job['old'], job['new']

                # The live id for this name wins over the CSV's LABEL_ID. The
                # rename is addressed by id, so a stale or hand-edited
                # LABEL_ID that now belongs to some other label would rename
                # THAT label -- and the log (the only undo record) would then
                # say the wrong original name. If the CSV carries an id, it
                # must agree with Gmail's; a mismatch is skipped rather than
                # guessed at.
                live_id = id_by_name.get(old)
                if not live_id:
                    print('SKIP    %s  (label no longer exists in Gmail)' % old)
                    record(old, new, 'SKIPPED', 'label no longer exists')
                    failed += 1
                    continue
                if job['id'] and job['id'] != live_id:
                    print('SKIP    %s  (LABEL_ID %s in the CSV no longer matches this '
                          'label, which is now %s - re-run audit.py)'
                          % (old, job['id'], live_id))
                    record(old, new, 'SKIPPED', 'LABEL_ID no longer matches LABEL')
                    failed += 1
                    continue
                label_id = live_id

                if new in existing:
                    print('SKIP    %s  ->  %s  (target already exists)' % (old, new))
                    record(old, new, 'SKIPPED', 'target already exists')
                    failed += 1
                    continue

                # Make sure the intermediate archive container exists as a
                # real label.
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
                                record(old, new, 'FAILED', 'container: %s' % err)
                                failed += 1
                                continue
                    existing.add(container)
                    created += 1

                print('%sMOVE    %s  ->  %s' % (tag, old, new))
                if dry:
                    record(old, new, 'DRY-RUN', '')
                    planned += 1
                    continue

                try:
                    rename_label(svc, label_id, new)
                    existing.discard(old)
                    existing.add(new)
                    id_by_name[new] = label_id
                    record(old, new, 'DONE', '')
                    moved += 1
                    time.sleep(0.12)          # stay well inside Gmail API rate limits
                except HttpError as err:
                    print('FAILED  %s : %s' % (old, err))
                    record(old, new, 'FAILED', str(err))
                    failed += 1
        except BaseException:
            # Anything the loop doesn't handle itself: a network failure,
            # Ctrl-C. Say where the partial record is before the traceback.
            if not dry:
                print('\nSTOPPED EARLY after %d rename(s). KEEP %s - it records '
                      'every rename made before the stop.' % (moved, log_path))
            raise

    print()
    if dry:
        print('DRY RUN finished - nothing was changed.')
        print('%d rename(s) and %d new container(s) would happen; %d row(s) would '
              'be skipped.' % (planned, created, failed))
        if old_csv:
            print('WARNING: %s' % warning)
            print('--apply will refuse this CSV unless you add --allow-old-csv.')
        else:
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
