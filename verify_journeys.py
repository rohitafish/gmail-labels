#!/usr/bin/env python3
"""
Verification: diff a fresh Journeys audit against the hand-checked spreadsheet.

    python3 audit.py --only Journeys --out journeys_audit.csv
    python3 verify_journeys.py journeys_audit.csv

Compares label coverage, VERDICT and last-email date against
"Gmail label audit - Journeys.xlsx". Reads the xlsx with the standard library
only - it is a zip of XML, no third-party reader needed.

Note the two audits measure slightly different things. The spreadsheet used
thread dates (the last message of the newest thread carrying the label, which
may itself be unlabelled); this audit uses message dates (the newest message
that actually carries the label). Where they differ, the message date is the
stricter and more correct answer, and small date gaps are expected.
"""

import csv
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta

import gmail_common as gc

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
XLSX = os.path.join(gc.HERE, 'Gmail label audit - Journeys.xlsx')
EXCEL_EPOCH = date(1899, 12, 30)


def read_xlsx(path):
    """Rows of the first worksheet as lists of strings."""
    zf = zipfile.ZipFile(path)

    shared = []
    try:
        root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
        shared = [''.join(t.text or '' for t in si.iter(NS + 't')) for si in root]
    except KeyError:
        pass

    sheet = ET.fromstring(zf.read('xl/worksheets/sheet1.xml'))
    rows = []
    for row in sheet.iter(NS + 'row'):
        cells = {}
        for cell in row.findall(NS + 'c'):
            column = re.match(r'[A-Z]+', cell.get('r')).group()
            inline = cell.find(NS + 'is')
            value = cell.find(NS + 'v')
            if inline is not None:
                text = ''.join(t.text or '' for t in inline.iter(NS + 't'))
            elif value is None:
                text = ''
            elif cell.get('t') == 's':
                text = shared[int(value.text)]
            else:
                text = value.text
            cells[column] = text
        rows.append([cells.get(c, '') for c in 'ABCDEFG'])
    return rows


def serial_to_date(value):
    try:
        return EXCEL_EPOCH + timedelta(days=int(float(value)))
    except (TypeError, ValueError):
        return None


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(gc.HERE, 'journeys_audit.csv')
    if not os.path.exists(csv_path):
        sys.exit('No audit CSV at %s\nRun:  python3 audit.py --only Journeys --out %s'
                 % (csv_path, csv_path))
    if not os.path.exists(XLSX):
        sys.exit('Missing reference spreadsheet: %s' % XLSX)

    reference = {}
    for row in read_xlsx(XLSX):
        label = row[0]
        if not label.startswith('Journeys') or '/' not in label:
            continue                      # header, summary and note rows
        if gc.is_archived(label):
            continue                      # free-text notes mentioning Old/* paths
        reference[label] = {'date': serial_to_date(row[1]), 'verdict': row[3]}

    with open(csv_path, encoding='utf-8-sig') as fh:
        mine = {
            r['LABEL']: r for r in csv.DictReader(fh)
            if r['LEAF'] == 'yes' and not gc.is_archived(r['LABEL'])
        }

    print('Reference spreadsheet : %d leaf labels' % len(reference))
    print('This audit            : %d leaf labels\n' % len(mine))

    problems = 0

    missing_from_ref = sorted(set(mine) - set(reference))
    if missing_from_ref:
        problems += len(missing_from_ref)
        print('IN THIS AUDIT BUT NOT IN THE SPREADSHEET (%d):' % len(missing_from_ref))
        for label in missing_from_ref:
            print('  %s   (%s messages, last %s)'
                  % (label, mine[label]['MESSAGES'], mine[label]['LAST_EMAIL'] or 'never'))
        print()

    missing_from_mine = sorted(set(reference) - set(mine))
    if missing_from_mine:
        problems += len(missing_from_mine)
        print('IN THE SPREADSHEET BUT NOT IN THIS AUDIT (%d):' % len(missing_from_mine))
        for label in missing_from_mine:
            print('  %s' % label)
        print()

    verdict_diffs = []
    date_diffs = []
    for label in sorted(set(mine) & set(reference)):
        ref, got = reference[label], mine[label]

        # The spreadsheet has no separate borderline verdict; compare the
        # stale/active call itself, not the ACTION we defaulted it to.
        ref_stale = ref['verdict'].startswith('STALE')
        got_stale = got['VERDICT'].startswith('STALE')
        if ref_stale != got_stale:
            verdict_diffs.append((label, ref['verdict'], got['VERDICT'],
                                  ref['date'], got['LAST_EMAIL']))

        if ref['date'] and got['LAST_EMAIL']:
            delta = abs((datetime.fromisoformat(got['LAST_EMAIL']).date() - ref['date']).days)
            if delta > 1:
                date_diffs.append((label, ref['date'], got['LAST_EMAIL'], delta))

    if verdict_diffs:
        problems += len(verdict_diffs)
        print('VERDICT MISMATCHES (%d):' % len(verdict_diffs))
        for label, ref_v, got_v, ref_d, got_d in verdict_diffs:
            print('  %s\n    spreadsheet: %-8s (%s)\n    this audit : %-8s (%s)'
                  % (label, ref_v, ref_d, got_v, got_d))
        print()

    if date_diffs:
        print('DATE DIFFERENCES OVER 1 DAY (%d) - verdict unaffected:' % len(date_diffs))
        for label, ref_d, got_d, delta in date_diffs:
            print('  %-45s spreadsheet %s vs audit %s  (%d days)'
                  % (label, ref_d, got_d, delta))
        print()

    matched = len(set(mine) & set(reference)) - len(verdict_diffs)
    print('--- RESULT ---')
    print('Verdicts agreeing: %d of %d shared labels' % (matched, len(set(mine) & set(reference))))
    if problems == 0:
        print('Clean match. The logic agrees with your hand-checked audit.')
        return 0
    print('%d discrepancy/discrepancies above - resolve before applying at scale.' % problems)
    return 1


if __name__ == '__main__':
    sys.exit(main())
