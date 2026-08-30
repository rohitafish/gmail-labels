"""Covers audit.main() end-to-end against a fake Gmail service: argument
handling (--only, --no-revive, --out), the CSV it writes, and how the cache
changes what gets called. classify() itself is covered in
test_audit_classify.py; this module is about the wiring around it -- does
main() call the right things, in the right order, and write what it says it
wrote.

No real Gmail account, network call, or credentials.json is used anywhere
here: gc.service is monkeypatched to hand back a FakeGmailService directly,
so main() never reaches the OAuth path at all.
"""

import csv
import sys
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_label, utc

import audit
import gmail_common as gc


@pytest.fixture(autouse=True)
def _unrestricted_scope(monkeypatch):
    """The real SCOPE_PREFIXES (Dosh/Politics/Business/...) has nothing to
    do with what these tests exercise -- widen it so fabricated names like
    'Widgets 🧩/...' are in scope without every test having to know that
    default exists."""
    monkeypatch.setattr(gc, 'SCOPE_PREFIXES', [])


def _run_main(monkeypatch, fake_service, out_path, extra_argv=()):
    monkeypatch.setattr(audit.gc, 'service', lambda scope, token_filename: fake_service)
    monkeypatch.setattr(sys, 'argv', ['audit.py', '--out', str(out_path), *extra_argv])
    audit.main()
    with open(out_path, encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh))


def test_header_matches_csv_columns_exactly(tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [make_label('Widgets 🧩/Vendors/Acme Supply')]
    out = tmp_path / 'audit.csv'

    _run_main(monkeypatch, fake_service, out)

    with open(out, encoding='utf-8-sig') as fh:
        header = next(csv.reader(fh))
    assert header == gc.CSV_COLUMNS


def test_the_csv_has_a_utf8_bom_so_excel_opens_emoji_names_correctly(tmp_path, monkeypatch,
                                                                     fake_service):
    fake_service.label_list = [make_label('Widgets 🧩/Vendors/Acme Supply')]
    out = tmp_path / 'audit.csv'

    _run_main(monkeypatch, fake_service, out)

    raw = out.read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')


def test_rows_are_sorted_oldest_first_with_blanks_last(tmp_path, monkeypatch, fake_service):
    now = utc(2026, 8, 30)
    fake_service.label_list = [
        make_label('Widgets 🧩/Vendors/Acme Supply', 'id-recent'),
        make_label('Widgets 🧩/Vendors/Bramble Ltd', 'id-old'),
        make_label('Widgets 🧩/Vendors/Cinder Co', 'id-empty'),
    ]
    fake_service.mail = {
        'id-recent': (now - timedelta(days=5), 3),
        'id-old': (now - timedelta(days=2000), 9),
        'id-empty': (None, 0),
    }
    out = tmp_path / 'audit.csv'

    rows = _run_main(monkeypatch, fake_service, out)

    assert [r['LABEL'] for r in rows] == [
        'Widgets 🧩/Vendors/Bramble Ltd',    # oldest first
        'Widgets 🧩/Vendors/Acme Supply',
        'Widgets 🧩/Vendors/Cinder Co',       # never had mail - AGE_DAYS blank, sorts last
    ]


def test_only_restricts_to_labels_starting_with_the_given_prefix(tmp_path, monkeypatch,
                                                                  fake_service):
    fake_service.label_list = [
        make_label('Widgets 🧩/Vendors/Acme Supply'),
        make_label('Gadgets 🔧/Vendors/Other Co'),
    ]
    out = tmp_path / 'audit.csv'

    rows = _run_main(monkeypatch, fake_service, out, extra_argv=['--only', 'Widgets'])

    assert [r['LABEL'] for r in rows] == ['Widgets 🧩/Vendors/Acme Supply']


def test_no_revive_drops_archived_labels_entirely(tmp_path, monkeypatch, fake_service):
    fake_service.label_list = [
        make_label('Widgets 🧩/Vendors/Acme Supply'),
        make_label('Widgets 🧩/Old/Bramble Ltd'),
    ]
    out = tmp_path / 'audit.csv'

    rows = _run_main(monkeypatch, fake_service, out, extra_argv=['--no-revive'])

    assert [r['LABEL'] for r in rows] == ['Widgets 🧩/Vendors/Acme Supply']


def test_a_collision_is_counted_and_reported(tmp_path, monkeypatch, fake_service, capsys):
    """classify()'s COLLISION flag is exercised directly in
    test_audit_classify.py; this pins that main() actually wires it into
    the run-level counters and the printed summary."""
    now = datetime.now(UTC)
    old = now - timedelta(days=365.2425 * gc.STALE_YEARS + 400)
    fake_service.label_list = [
        make_label('Widgets 🧩/Vendors/Acme Supply', 'id-1'),
        make_label('Widgets 🧩/Vendors/Old/Acme Supply', 'id-2'),  # the proposed target
    ]
    fake_service.mail = {'id-1': (old, 3), 'id-2': (old, 1)}
    out = tmp_path / 'audit.csv'

    _run_main(monkeypatch, fake_service, out)

    assert 'Name collisions (skipped)      1' in capsys.readouterr().out


def test_a_borderline_result_is_counted_and_reported(tmp_path, monkeypatch, fake_service, capsys):
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=365.2425 * gc.STALE_YEARS)
    stale_days = (now - cutoff).days
    borderline_last = now - timedelta(days=stale_days)  # exactly on the line
    fake_service.label_list = [make_label('Widgets 🧩/Vendors/Acme Supply', 'id-1')]
    fake_service.mail = {'id-1': (borderline_last, 3)}
    out = tmp_path / 'audit.csv'

    _run_main(monkeypatch, fake_service, out)

    assert 'Borderline (defaulted to SKIP) 1' in capsys.readouterr().out


def test_revive_cutoff_is_wired_through_from_main_not_just_classify(tmp_path, monkeypatch,
                                                                     fake_service):
    """classify()'s 6-month revive window is exercised directly in
    test_audit_classify.py; this confirms main() actually computes
    revive_cutoff from gc.REVIVE_MONTHS and passes it through, rather than,
    say, leaving classify() to default it or main() forgetting to thread it
    -- an archived label with mail well outside 6 months but inside the old
    2-year window must come back as ARCHIVED via a real main() run."""
    now = datetime.now(UTC)
    revive_cutoff = now - timedelta(days=365.2425 * gc.REVIVE_MONTHS / 12)
    older_than_revive_window = revive_cutoff - timedelta(days=60)
    fake_service.label_list = [make_label('Widgets 🧩/Old/Acme Supply', 'id-1')]
    fake_service.mail = {'id-1': (older_than_revive_window, 3)}
    out = tmp_path / 'audit.csv'

    rows = _run_main(monkeypatch, fake_service, out)

    assert rows[0]['VERDICT'] == 'ARCHIVED'


def test_progress_is_reported_every_25_fresh_lookups(tmp_path, monkeypatch, fake_service,
                                                       capsys):
    fake_service.label_list = [
        make_label('Widgets 🧩/Vendors/Label%02d' % i, 'id-%d' % i) for i in range(26)
    ]
    out = tmp_path / 'audit.csv'

    _run_main(monkeypatch, fake_service, out)

    assert '...25/26 examined' in capsys.readouterr().out


def test_a_cache_hit_issues_no_messages_calls_at_all(tmp_path, monkeypatch, fake_service):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    label = make_label('Widgets 🧩/Vendors/Acme Supply', 'id-1')
    fake_service.label_list = [label]

    now = utc(2026, 8, 30)
    audit.save_cache({'id-1': {'last': (now - timedelta(days=5)).isoformat(), 'messages': 3}})

    out = tmp_path / 'audit.csv'
    _run_main(monkeypatch, fake_service, out)

    assert not any(c.startswith('messages.') for c in fake_service.calls)
    assert 'labels.list' in fake_service.calls
