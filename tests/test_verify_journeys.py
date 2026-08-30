"""Covers verify_journeys.py: serial_to_date, read_xlsx's OOXML parsing, and
main()'s diff logic. This script currently cannot run at all on a fresh
clone -- its XLSX constant points at a gitignored spreadsheet that only
exists on the machine that ran the original verification -- so every test
here builds a small synthetic .xlsx by hand with zipfile + string XML
(the same "it's a zip of XML, no third-party reader needed" approach the
module itself takes) and monkeypatches the XLSX constant to point at it.

read_xlsx handles two different cell encodings for text (shared strings,
referenced by index, and inline strings, embedded directly in the cell) --
tests cover both, since a spreadsheet exported by a different tool could use
either.
"""

import csv
import sys
import zipfile
from datetime import date

import pytest

import gmail_common as gc
import verify_journeys as vj


def _make_xlsx(path, rows):
    """rows: list of {column_letter: (kind, value)} dicts, one per XLSX row
    (1-indexed). kind is 's' (shared string), 'inline' (inline string), 'n'
    (bare numeric literal, e.g. a date serial), or 'empty' (a cell tag with
    neither a <v> nor an <is> child -- value is ignored)."""
    shared = []
    shared_index = {}

    def shared_ref(text):
        if text not in shared_index:
            shared_index[text] = len(shared)
            shared.append(text)
        return shared_index[text]

    row_xml = []
    for r, cells in enumerate(rows, start=1):
        cell_xml = []
        for col, (kind, value) in cells.items():
            ref = '%s%d' % (col, r)
            if kind == 's':
                cell_xml.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, shared_ref(value)))
            elif kind == 'inline':
                cell_xml.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, value))
            elif kind == 'empty':
                cell_xml.append('<c r="%s"/>' % ref)
            else:
                cell_xml.append('<c r="%s"><v>%s</v></c>' % (ref, value))  # bare numeric
        row_xml.append('<row r="%d">%s</row>' % (r, ''.join(cell_xml)))

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>%s</sheetData></worksheet>' % ''.join(row_xml)
    )

    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('xl/worksheets/sheet1.xml', sheet_xml)
        if shared:
            sst_items = ''.join('<si><t>%s</t></si>' % s for s in shared)
            sst_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'count="%d" uniqueCount="%d">%s</sst>' % (len(shared), len(shared), sst_items)
            )
            zf.writestr('xl/sharedStrings.xml', sst_xml)


# ---------------------------- serial_to_date ----------------------------

def test_serial_to_date_uses_the_1899_12_30_epoch():
    assert vj.serial_to_date('2') == date(1900, 1, 1)


def test_serial_to_date_accepts_a_float_string():
    assert vj.serial_to_date('2.0') == date(1900, 1, 1)


def test_serial_to_date_returns_none_for_garbage():
    assert vj.serial_to_date('not-a-number') is None


def test_serial_to_date_returns_none_for_none():
    assert vj.serial_to_date(None) is None


# ---------------------------- read_xlsx ----------------------------

def test_read_xlsx_resolves_shared_string_cells(tmp_path):
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [
        {'A': ('s', 'Journeys✈️/Car/Taxis/RideCo'), 'B': ('n', '46000'),
         'D': ('s', 'ACTIVE')},
    ])

    rows = vj.read_xlsx(xlsx)

    assert rows[0][0] == 'Journeys✈️/Car/Taxis/RideCo'
    assert rows[0][1] == '46000'
    assert rows[0][3] == 'ACTIVE'


def test_read_xlsx_resolves_inline_string_cells_with_no_shared_strings_part(tmp_path):
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [
        {'A': ('inline', 'Journeys✈️/Car/Taxis/RideCo'), 'B': ('n', '46000'),
         'D': ('inline', 'STALE')},
    ])

    rows = vj.read_xlsx(xlsx)

    assert rows[0][0] == 'Journeys✈️/Car/Taxis/RideCo'
    assert rows[0][3] == 'STALE'


def test_read_xlsx_pads_missing_columns_with_empty_string(tmp_path):
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [{'A': ('s', 'Journeys✈️/Car/Taxis/RideCo')}])

    rows = vj.read_xlsx(xlsx)

    assert rows[0] == ['Journeys✈️/Car/Taxis/RideCo', '', '', '', '', '', '']


def test_read_xlsx_reads_a_genuinely_empty_cell_as_empty_string(tmp_path):
    """A cell tag can be present with neither a <v> nor an <is> child (a
    blank cell Excel still emits a reference for) -- distinct from the
    column being absent entirely, which read_xlsx.get(c, '') also
    defaults to ''."""
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [{'A': ('s', 'Journeys✈️/Car/Taxis/RideCo'), 'B': ('empty', None)}])

    rows = vj.read_xlsx(xlsx)

    assert rows[0][1] == ''


# ---------------------------- main() ----------------------------

def _write_audit_csv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=gc.CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _audit_row(label, verdict, last_email, leaf='yes'):
    return {
        'LABEL': label, 'LABEL_ID': 'id-1', 'LAST_EMAIL': last_email, 'AGE_DAYS': 10,
        'MESSAGES': 1, 'LEAF': leaf, 'VERDICT': verdict, 'PROPOSED_NEW_NAME': '',
        'ACTION': 'SKIP', 'NOTE': '',
    }


LABEL = 'Journeys✈️/Car/Taxis/RideCo'


def test_a_clean_match_exits_zero(tmp_path, monkeypatch, capsys):
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [{'A': ('s', LABEL), 'B': ('n', '46000'), 'D': ('s', 'ACTIVE')}])
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))

    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [_audit_row(LABEL, 'ACTIVE', '2025-12-09')])
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    exit_code = vj.main()

    assert exit_code == 0
    assert 'Clean match' in capsys.readouterr().out


def test_a_label_in_the_audit_but_not_the_spreadsheet_is_a_problem(tmp_path, monkeypatch,
                                                                   capsys):
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [])  # nothing in the reference at all
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))

    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [_audit_row(LABEL, 'ACTIVE', '2025-12-25')])
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    exit_code = vj.main()

    assert exit_code == 1
    assert 'IN THIS AUDIT BUT NOT IN THE SPREADSHEET' in capsys.readouterr().out


def test_a_label_in_the_spreadsheet_but_not_the_audit_is_a_problem(tmp_path, monkeypatch,
                                                                    capsys):
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [{'A': ('s', LABEL), 'B': ('n', '46000'), 'D': ('s', 'ACTIVE')}])
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))

    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [])  # empty audit
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    exit_code = vj.main()

    assert exit_code == 1
    assert 'IN THE SPREADSHEET BUT NOT IN THIS AUDIT' in capsys.readouterr().out


def test_a_verdict_mismatch_is_a_problem(tmp_path, monkeypatch, capsys):
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [{'A': ('s', LABEL), 'B': ('n', '46000'), 'D': ('s', 'ACTIVE')}])
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))

    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [_audit_row(LABEL, 'STALE', '2020-01-01')])
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    exit_code = vj.main()

    assert exit_code == 1
    assert 'VERDICT MISMATCHES' in capsys.readouterr().out


def test_a_date_difference_over_one_day_is_reported_but_not_a_problem(tmp_path, monkeypatch,
                                                                      capsys):
    """verify_journeys.py's own documented design: the spreadsheet used
    thread dates, this audit uses message dates, so a date gap alone
    (verdict still agreeing) is informational, not a failure -- main() never
    adds date_diffs to `problems`."""
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [{'A': ('s', LABEL), 'B': ('n', '46000'), 'D': ('s', 'ACTIVE')}])
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))

    # serial 46000 -> 2025-12-09 (EXCEL_EPOCH + 46000 days); pick an audit
    # date several days off but on the same side of the stale/active line.
    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [_audit_row(LABEL, 'ACTIVE', '2025-12-01')])
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    exit_code = vj.main()

    out = capsys.readouterr().out
    assert exit_code == 0
    assert 'DATE DIFFERENCES OVER 1 DAY' in out


def test_archived_leaves_are_excluded_from_both_sides(tmp_path, monkeypatch, capsys):
    """An archived label present on only one side must not be flagged as a
    coverage gap -- it's excluded from both the reference and the audit
    comparison sets before the diff runs."""
    archived_label = 'Journeys✈️/Old/Car Hire'
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [
        {'A': ('s', LABEL), 'B': ('n', '46000'), 'D': ('s', 'ACTIVE')},
    ])
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))

    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [
        _audit_row(LABEL, 'ACTIVE', '2025-12-09'),
        _audit_row(archived_label, 'ARCHIVED', '2018-01-01'),
    ])
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    exit_code = vj.main()

    assert exit_code == 0
    assert 'Clean match' in capsys.readouterr().out


def test_reference_rows_not_shaped_like_a_leaf_label_are_skipped(tmp_path, monkeypatch, capsys):
    """The reference-building loop skips any row whose column A doesn't
    start with 'Journeys' or has no '/' -- header rows, blank rows, and
    free-text summary notes in the spreadsheet all look like this."""
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [
        {'A': ('s', 'LABEL'), 'B': ('s', 'LAST_EMAIL')},          # a header row
        {'A': ('s', 'Total: 62 leaves reviewed')},                # a summary note, no '/'
        {'A': ('s', LABEL), 'B': ('n', '46000'), 'D': ('s', 'ACTIVE')},
    ])
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))

    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [_audit_row(LABEL, 'ACTIVE', '2025-12-09')])
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    exit_code = vj.main()

    assert exit_code == 0
    assert 'Clean match' in capsys.readouterr().out


def test_an_archived_label_in_the_reference_spreadsheet_itself_is_skipped(tmp_path, monkeypatch,
                                                                          capsys):
    """Distinct from the audit-side exclusion covered above: a free-text
    note in the spreadsheet mentioning an Old/* path is excluded from the
    *reference* dict before any comparison happens, so it never becomes a
    'missing from audit' coverage gap."""
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [
        {'A': ('s', LABEL), 'B': ('n', '46000'), 'D': ('s', 'ACTIVE')},
        {'A': ('s', 'Journeys✈️/Old/Car Hire'), 'B': ('n', '40000'), 'D': ('s', 'ARCHIVED')},
    ])
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))

    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [_audit_row(LABEL, 'ACTIVE', '2025-12-09')])
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    exit_code = vj.main()

    assert exit_code == 0
    assert 'Clean match' in capsys.readouterr().out


def test_a_matched_label_with_no_date_on_either_side_skips_the_date_comparison(
        tmp_path, monkeypatch, capsys):
    """When a shared label never had mail on either side (ref['date'] is
    None because the cell was blank/unparseable, got['LAST_EMAIL'] is '' for
    an EMPTY verdict), the date-diff check is skipped entirely rather than
    comparing None against ''."""
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [{'A': ('s', LABEL), 'B': ('empty', None), 'D': ('s', 'EMPTY')}])
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))

    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [_audit_row(LABEL, 'EMPTY', '')])
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    exit_code = vj.main()

    out = capsys.readouterr().out
    assert exit_code == 0
    assert 'DATE DIFFERENCES' not in out
    assert 'Clean match' in out


def test_non_leaf_rows_in_the_audit_are_ignored(tmp_path, monkeypatch, capsys):
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [{'A': ('s', LABEL), 'B': ('n', '46000'), 'D': ('s', 'ACTIVE')}])
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))

    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [
        _audit_row(LABEL, 'ACTIVE', '2025-12-09'),
        _audit_row('Journeys✈️/Car', 'ACTIVE (has sub-labels)', '2025-12-09', leaf='no'),
    ])
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    exit_code = vj.main()

    assert exit_code == 0
    assert 'Clean match' in capsys.readouterr().out


def test_missing_csv_exits_with_a_pointer_to_generate_it(tmp_path, monkeypatch):
    xlsx = tmp_path / 'ref.xlsx'
    _make_xlsx(xlsx, [])
    monkeypatch.setattr(vj, 'XLSX', str(xlsx))
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(tmp_path / 'missing.csv')])

    with pytest.raises(SystemExit) as excinfo:
        vj.main()
    assert 'audit.py --only Journeys' in str(excinfo.value)


def test_missing_xlsx_exits_with_a_clear_message(tmp_path, monkeypatch):
    monkeypatch.setattr(vj, 'XLSX', str(tmp_path / 'does-not-exist.xlsx'))
    csv_path = tmp_path / 'journeys_audit.csv'
    _write_audit_csv(csv_path, [])
    monkeypatch.setattr(sys, 'argv', ['verify_journeys.py', str(csv_path)])

    with pytest.raises(SystemExit) as excinfo:
        vj.main()
    assert 'Missing reference spreadsheet' in str(excinfo.value)
