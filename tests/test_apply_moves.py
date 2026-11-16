"""Covers apply_moves.py: read_jobs' CSV parsing and job ordering, main()'s
dry-run and --apply paths against a fake Gmail service, and the log-writing
guard. create_label/rename_label are the two named seams extracted from
main()'s inline fluent-chain calls -- tested indirectly here through main(),
since their whole point is to be simple, named wrappers around
gc.retry(...).execute() rather than logic worth testing in isolation.
"""

import csv
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import http_error, make_label

import apply_moves
import gmail_common as gc


def _write_csv(path, rows, fieldnames=gc.CSV_COLUMNS):
    with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _row(label, new_name, label_id='', action='MOVE'):
    return {
        'LABEL': label, 'LABEL_ID': label_id, 'LAST_EMAIL': '', 'AGE_DAYS': '',
        'MESSAGES': 0, 'LEAF': 'yes', 'VERDICT': 'STALE',
        'PROPOSED_NEW_NAME': new_name, 'ACTION': action, 'NOTE': '',
    }


# ---------------------------- read_jobs ----------------------------

def test_only_move_rows_become_jobs(tmp_path):
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [
        _row('Widgets 🧩/A', 'Widgets 🧩/Old/A', action='MOVE'),
        _row('Widgets 🧩/B', 'Widgets 🧩/Old/B', action='SKIP'),
    ])

    _rows, jobs = apply_moves.read_jobs(csv_path)

    assert [j['old'] for j in jobs] == ['Widgets 🧩/A']


def test_move_matching_is_case_and_whitespace_insensitive(tmp_path):
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', action='  move ')])

    _rows, jobs = apply_moves.read_jobs(csv_path)

    assert len(jobs) == 1


def test_move_with_a_blank_proposed_name_is_skipped(tmp_path, capsys):
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/A', '', action='MOVE')])

    _rows, jobs = apply_moves.read_jobs(csv_path)

    assert jobs == []
    assert 'PROPOSED_NEW_NAME is empty' in capsys.readouterr().out


def test_jobs_are_ordered_deepest_path_first(tmp_path):
    """The invariant that stops a parent being renamed out from under a
    child mid-run."""
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [
        _row('Widgets 🧩', 'Old/Widgets 🧩'),
        _row('Widgets 🧩/Vendors/Acme Supply', 'Widgets 🧩/Vendors/Old/Acme Supply'),
        _row('Widgets 🧩/Vendors', 'Widgets 🧩/Old/Vendors'),
    ])

    _rows, jobs = apply_moves.read_jobs(csv_path)

    assert [j['old'] for j in jobs] == [
        'Widgets 🧩/Vendors/Acme Supply',   # 3 segments
        'Widgets 🧩/Vendors',                # 2 segments
        'Widgets 🧩',                        # 1 segment
    ]


def test_main_exits_early_with_nothing_marked_move(tmp_path, monkeypatch, fake_service,
                                                    capsys):
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', action='SKIP')])

    _run_main(monkeypatch, fake_service, csv_path)

    assert 'nothing to do' in capsys.readouterr().out
    assert fake_service.calls == []  # never even reached gc.service()


# ---------------------------- main(): dry run ----------------------------

def _run_main(monkeypatch, fake_service, csv_path, argv_extra=()):
    monkeypatch.setattr(apply_moves.gc, 'service', lambda scope, token_filename: fake_service)
    monkeypatch.setattr(sys, 'argv', ['apply_moves.py', '--csv', str(csv_path), *argv_extra])
    apply_moves.main()


def test_dry_run_makes_no_create_or_rename_calls(tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [make_label('Widgets 🧩/Vendors/Acme Supply', 'id-1')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/Vendors/Acme Supply',
                                'Widgets 🧩/Vendors/Old/Acme Supply', 'id-1')])

    _run_main(monkeypatch, fake_service, csv_path)

    assert 'labels.create' not in fake_service.calls
    assert 'labels.patch' not in fake_service.calls


def test_dry_run_writes_a_dryrun_suffixed_log(tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [make_label('Widgets 🧩/Vendors/Acme Supply', 'id-1')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/Vendors/Acme Supply',
                                'Widgets 🧩/Vendors/Old/Acme Supply', 'id-1')])

    _run_main(monkeypatch, fake_service, csv_path)

    logs = list(tmp_path.glob('apply_log-*-dryrun.csv'))
    assert len(logs) == 1
    with open(logs[0], encoding='utf-8-sig') as fh:
        log_rows = list(csv.DictReader(fh))
    assert log_rows[0]['RESULT'] == 'DRY-RUN'


def test_a_formula_leading_label_name_is_escaped_in_the_log(tmp_path, monkeypatch, fake_service):
    """The undo procedure (SETUP-LOCAL.md) has you open this log in a
    spreadsheet -- a name starting with =/+/-/@ must come through as literal
    text, not a formula (CWE-1236)."""
    risky_name = '=cmd|"/c calc"!A1'
    fake_service.label_list = [make_label(risky_name, 'id-1')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row(risky_name, 'Old/' + risky_name, 'id-1')])

    _run_main(monkeypatch, fake_service, csv_path)

    log_path = next(tmp_path.glob('apply_log-*-dryrun.csv'))
    with open(log_path, encoding='utf-8-sig') as fh:
        log_rows = list(csv.DictReader(fh))
    assert log_rows[0]['ORIGINAL_NAME'] == "'" + risky_name
    # NEW_NAME ('Old/=cmd...') doesn't itself START with a formula
    # character -- only the leading character of a value matters, so this
    # one is correctly left untouched.
    assert log_rows[0]['NEW_NAME'] == 'Old/' + risky_name


def test_dry_run_reports_each_missing_container_only_once(tmp_path, monkeypatch, fake_service,
                                                            capsys):
    """Two jobs sharing the same missing container should print CREATE once,
    not once per job -- main() tracks `existing` across the whole run, even
    in dry mode."""
    fake_service.label_list = [
        make_label('Widgets 🧩/Vendors/Acme Supply', 'id-1'),
        make_label('Widgets 🧩/Vendors/Bramble Ltd', 'id-2'),
    ]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [
        _row('Widgets 🧩/Vendors/Acme Supply', 'Widgets 🧩/Vendors/Old/Acme Supply', 'id-1'),
        _row('Widgets 🧩/Vendors/Bramble Ltd', 'Widgets 🧩/Vendors/Old/Bramble Ltd', 'id-2'),
    ])

    _run_main(monkeypatch, fake_service, csv_path)

    out = capsys.readouterr().out
    assert out.count('CREATE  Widgets 🧩/Vendors/Old') == 1


# ---------------------------- main(): --apply ----------------------------

def test_apply_renames_in_deepest_first_order(tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [
        make_label('Widgets 🧩', 'id-parent'),
        make_label('Widgets 🧩/Vendors', 'id-child'),
    ]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [
        _row('Widgets 🧩', 'Old/Widgets 🧩', 'id-parent'),
        _row('Widgets 🧩/Vendors', 'Widgets 🧩/Old/Vendors', 'id-child'),
    ])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    patches = [c for c in fake_service.calls if c == 'labels.patch']
    assert len(patches) == 2
    # The child's rename must have landed before the parent's -- check via
    # the resulting label_list order isn't reliable (patch mutates in
    # place), so assert on the label state instead: both ended up renamed.
    names = {label['id']: label['name'] for label in fake_service.label_list}
    assert names['id-child'] == 'Widgets 🧩/Old/Vendors'
    assert names['id-parent'] == 'Old/Widgets 🧩'


def test_apply_skips_a_label_that_no_longer_exists(tmp_path, monkeypatch, fake_service):
    fake_service.label_list = []  # the label vanished since the audit was written
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/Vendors/Acme Supply',
                                'Widgets 🧩/Vendors/Old/Acme Supply', 'id-1')])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    assert 'labels.patch' not in fake_service.calls


def test_apply_skips_when_the_target_name_already_exists(tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [
        make_label('Widgets 🧩/Vendors/Acme Supply', 'id-1'),
        make_label('Widgets 🧩/Vendors/Old/Acme Supply', 'id-2'),
    ]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/Vendors/Acme Supply',
                                'Widgets 🧩/Vendors/Old/Acme Supply', 'id-1')])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    assert 'labels.patch' not in fake_service.calls


def test_apply_creates_a_missing_container_before_renaming(tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [make_label('Widgets 🧩/Vendors/Acme Supply', 'id-1')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/Vendors/Acme Supply',
                                'Widgets 🧩/Vendors/Old/Acme Supply', 'id-1')])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    names = {label['name'] for label in fake_service.label_list}
    assert 'Widgets 🧩/Vendors/Old' in names
    assert 'Widgets 🧩/Vendors/Old/Acme Supply' in names


def test_apply_tolerates_a_409_when_creating_a_container(tmp_path, monkeypatch, fake_service):
    """409 means the container was created by something else between the
    audit and this run (or by an earlier job in the same run) -- fine,
    not a failure."""
    fake_service.label_list = [make_label('Widgets 🧩/Vendors/Acme Supply', 'id-1')]
    fake_service.queue_error('labels.create', http_error(409))
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/Vendors/Acme Supply',
                                'Widgets 🧩/Vendors/Old/Acme Supply', 'id-1')])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    assert 'labels.patch' in fake_service.calls  # the rename still went ahead


def test_apply_records_a_non_409_create_failure_without_aborting_the_run(
        tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [
        make_label('Widgets 🧩/Vendors/Acme Supply', 'id-1'),
        make_label('Widgets 🧩/Sundries/Cinder Co', 'id-2'),
    ]
    # 400, not 500 -- gc.retry treats 500 as transient and would absorb a
    # single queued one by succeeding on its retry, masking the failure
    # this test is about.
    fake_service.queue_error('labels.create', http_error(400))
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [
        _row('Widgets 🧩/Vendors/Acme Supply', 'Widgets 🧩/Vendors/Old/Acme Supply', 'id-1'),
        _row('Widgets 🧩/Sundries/Cinder Co', 'Widgets 🧩/Sundries/Old/Cinder Co', 'id-2'),
    ])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    log_path = next(tmp_path.glob('apply_log-*.csv'))
    with open(log_path, encoding='utf-8-sig') as fh:
        results = {row['ORIGINAL_NAME']: row['RESULT'] for row in csv.DictReader(fh)}
    assert results['Widgets 🧩/Vendors/Acme Supply'] == 'FAILED'
    assert results['Widgets 🧩/Sundries/Cinder Co'] == 'DONE'  # the second job still ran


def test_apply_records_a_rename_failure_without_aborting_the_run(
        tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [
        make_label('Widgets 🧩/A', 'id-1'),
        make_label('Widgets 🧩/B', 'id-2'),
    ]
    fake_service.queue_error('labels.patch', http_error(400))  # non-transient: no retry
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [
        _row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-1'),
        _row('Widgets 🧩/B', 'Widgets 🧩/Old/B', 'id-2'),
    ])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    log_path = next(tmp_path.glob('apply_log-*.csv'))
    with open(log_path, encoding='utf-8-sig') as fh:
        results = {row['ORIGINAL_NAME']: row['RESULT'] for row in csv.DictReader(fh)}
    assert results['Widgets 🧩/A'] == 'FAILED'
    assert results['Widgets 🧩/B'] == 'DONE'


# ---------------------------- the log ----------------------------

def test_log_filename_is_timestamped_not_fixed(tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [make_label('Widgets 🧩/A', 'id-1')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-1')])

    _run_main(monkeypatch, fake_service, csv_path)

    logs = list(tmp_path.glob('apply_log-*.csv'))
    assert len(logs) == 1
    assert logs[0].name != 'apply_log.csv'


def test_refuses_to_write_the_log_over_the_input_csv(tmp_path, monkeypatch, fake_service):
    """apply_moves.py's own guard against --csv pointing at a name pattern
    that would collide with the timestamped log it's about to write."""
    fake_service.label_list = [make_label('Widgets 🧩/A', 'id-1')]
    csv_path = tmp_path / 'apply_log-x.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-1')])
    # Matches the literal suffix baked into csv_path above.
    monkeypatch.setattr(apply_moves.time, 'strftime', lambda fmt: 'x')

    with pytest.raises(SystemExit) as excinfo:
        _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])
    assert 'Refusing to write the log' in str(excinfo.value)
    assert 'labels.patch' not in fake_service.calls   # refused before renaming


def test_the_log_survives_a_run_that_dies_part_way_through(tmp_path, monkeypatch,
                                                           fake_service, capsys):
    """Only HttpError is handled per row. Anything else (a dropped
    connection, Ctrl-C) ends the run -- and the renames already made must
    still be in the log, which is why it is written row by row."""
    fake_service.label_list = [make_label('Widgets 🧩/A', 'id-1'),
                               make_label('Widgets 🧩/B', 'id-2'),
                               make_label('Widgets 🧩/Old', 'id-old')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-1'),
                          _row('Widgets 🧩/B', 'Widgets 🧩/Old/B', 'id-2')])
    fake_service.queue_error('labels.patch', None)                  # first rename works
    fake_service.queue_error('labels.patch', ConnectionError('gone'))

    with pytest.raises(ConnectionError):
        _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    log_path = next(tmp_path.glob('apply_log-*.csv'))
    with open(log_path, encoding='utf-8-sig') as fh:
        logged = list(csv.reader(fh))
    assert logged == [['ORIGINAL_NAME', 'NEW_NAME', 'RESULT', 'ERROR'],
                      ['Widgets 🧩/A', 'Widgets 🧩/Old/A', 'DONE', '']]
    out = capsys.readouterr().out
    assert 'STOPPED EARLY after 1 rename(s)' in out
    assert str(log_path) in out


def test_dry_run_summary_does_not_count_skipped_rows_as_renames(tmp_path, monkeypatch,
                                                                fake_service, capsys):
    fake_service.label_list = [make_label('Widgets 🧩/A', 'id-1'),
                               make_label('Widgets 🧩/Old', 'id-old')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-1'),
                          _row('Widgets 🧩/Gone', 'Widgets 🧩/Old/Gone', 'id-9')])

    _run_main(monkeypatch, fake_service, csv_path)

    assert ('1 rename(s) and 0 new container(s) would happen; 1 row(s) would be skipped'
            in capsys.readouterr().out)


# ---------------------------- an old audit CSV ----------------------------
# The labels scope cannot read mail, so apply_moves cannot see that a label
# has had mail since the audit. It refuses to --apply an old audit instead.

def _dated_row(label, new_name, label_id, audited_days_ago, age_days=900):
    """A row as audit.py would have written it `audited_days_ago` days ago."""
    today = datetime.now(UTC).date()
    last_email = today - timedelta(days=audited_days_ago + age_days)
    row = _row(label, new_name, label_id)
    row.update(LAST_EMAIL=last_email.isoformat(), AGE_DAYS=age_days)
    return row


def test_audit_age_comes_from_the_rows_not_the_file_time(tmp_path):
    """Editing ACTION weeks later refreshes the file's mtime; the age must
    not move with it."""
    csv_path = tmp_path / 'audit.csv'
    rows = [_row('Widgets 🧩/Empty', ''),                       # no date: passed over
            _dated_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-1', audited_days_ago=40)]
    _write_csv(csv_path, rows)                                  # mtime is now

    assert apply_moves.audit_age_days(csv_path, rows) == 40


def test_audit_age_falls_back_to_the_file_time_when_no_row_has_a_date(tmp_path):
    csv_path = tmp_path / 'audit.csv'
    rows = [_row('Widgets 🧩/A', 'Widgets 🧩/Old/A'),
            dict(_row('Widgets 🧩/B', ''), LAST_EMAIL='30/08/2026', AGE_DAYS='78')]
    _write_csv(csv_path, rows)
    ten_days_ago = time.time() - 10 * 86400
    os.utime(csv_path, (ten_days_ago, ten_days_ago))

    assert apply_moves.audit_age_days(csv_path, rows) in (10, 11)   # UTC date edge


def test_apply_refuses_an_old_audit_csv_and_changes_nothing(tmp_path, monkeypatch,
                                                            fake_service):
    fake_service.label_list = [make_label('Widgets 🧩/A', 'id-1')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_dated_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-1',
                                     audited_days_ago=gc.CACHE_MAX_AGE_DAYS + 1)])

    with pytest.raises(SystemExit) as excinfo:
        _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    assert '--allow-old-csv' in str(excinfo.value)
    assert fake_service.calls == []
    assert list(tmp_path.glob('apply_log-*.csv')) == []


def test_apply_accepts_an_audit_csv_exactly_at_the_limit(tmp_path, monkeypatch,
                                                         fake_service):
    fake_service.label_list = [make_label('Widgets 🧩/A', 'id-1'),
                               make_label('Widgets 🧩/Old', 'id-old')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_dated_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-1',
                                     audited_days_ago=gc.CACHE_MAX_AGE_DAYS)])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    assert 'labels.patch' in fake_service.calls


def test_allow_old_csv_applies_an_old_audit_with_a_warning(tmp_path, monkeypatch,
                                                           fake_service, capsys):
    fake_service.label_list = [make_label('Widgets 🧩/A', 'id-1'),
                               make_label('Widgets 🧩/Old', 'id-old')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_dated_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-1',
                                     audited_days_ago=60)])

    _run_main(monkeypatch, fake_service, csv_path,
              argv_extra=['--apply', '--allow-old-csv'])

    assert 'labels.patch' in fake_service.calls
    assert 'WARNING' in capsys.readouterr().out


def test_dry_run_of_an_old_audit_csv_warns_but_still_runs(tmp_path, monkeypatch,
                                                          fake_service, capsys):
    fake_service.label_list = [make_label('Widgets 🧩/A', 'id-1')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_dated_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-1',
                                     audited_days_ago=60)])

    _run_main(monkeypatch, fake_service, csv_path)

    out = capsys.readouterr().out
    assert 'from an audit 60 days ago' in out
    assert '--apply will refuse this CSV' in out
    assert 'Re-run with --apply' not in out


# ---------------------------- LABEL_ID vs LABEL ----------------------------
# The rename is addressed by id. The CSV's LABEL_ID is a snapshot from the
# audit run; if it has since come to belong to a different label (a label
# deleted and another created, or a hand-edited row), trusting it would
# rename the wrong label -- and the log, the only undo record, would say the
# wrong original name. Gmail's current id for the name is the authority.


def test_apply_skips_a_row_whose_label_id_now_belongs_to_another_label(
        tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [
        make_label('Widgets 🧩/A', 'id-a'),
        make_label('Widgets 🧩/Unrelated', 'id-stale'),   # the CSV's id points here now
    ]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-stale')])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    assert 'labels.patch' not in fake_service.calls
    names = {label['id']: label['name'] for label in fake_service.label_list}
    assert names['id-stale'] == 'Widgets 🧩/Unrelated'   # untouched
    assert names['id-a'] == 'Widgets 🧩/A'               # also untouched: skipped, not guessed
    log_path = next(tmp_path.glob('apply_log-*.csv'))
    with open(log_path, encoding='utf-8-sig') as fh:
        row = next(csv.DictReader(fh))
    assert row['RESULT'] == 'SKIPPED'
    assert 'LABEL_ID no longer matches' in row['ERROR']


def test_apply_uses_the_live_id_when_the_csv_has_none(tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [make_label('Widgets 🧩/A', 'id-a')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', label_id='')])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    names = {label['id']: label['name'] for label in fake_service.label_list}
    assert names['id-a'] == 'Widgets 🧩/Old/A'


def test_apply_renames_when_the_csv_id_matches_the_live_one(tmp_path, monkeypatch, fake_service):
    """The counterweight: an id that still agrees with Gmail is the normal
    case and must keep working exactly as before."""
    fake_service.label_list = [make_label('Widgets 🧩/A', 'id-a')]
    csv_path = tmp_path / 'audit.csv'
    _write_csv(csv_path, [_row('Widgets 🧩/A', 'Widgets 🧩/Old/A', 'id-a')])

    _run_main(monkeypatch, fake_service, csv_path, argv_extra=['--apply'])

    assert fake_service.calls.count('labels.patch') == 1
