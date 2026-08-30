"""Covers audit.report_revivals and audit.report_fully_stale_branches --
the two report-only functions main() calls after the CSV is written. Both
take already-classified rows (CSV_COLUMNS-shaped dicts), so no fake service
or cache is needed here; conftest.make_row builds them directly.
"""

from conftest import make_row

from audit import report_fully_stale_branches, report_revivals

# ---------------------------- report_revivals ----------------------------

def test_no_revivals_prints_the_nothing_to_bring_back_message(capsys):
    report_revivals([make_row(VERDICT='ACTIVE')])

    out = capsys.readouterr().out
    assert 'Nothing to bring back' in out


def test_revivals_are_sorted_newest_mail_first(capsys):
    rows = [
        make_row(LABEL='Widgets 🧩/Old/Older Co', VERDICT='REVIVE', ACTION='MOVE',
                 AGE_DAYS=500, PROPOSED_NEW_NAME='Widgets 🧩/Older Co'),
        make_row(LABEL='Widgets 🧩/Old/Newer Co', VERDICT='REVIVE', ACTION='MOVE',
                 AGE_DAYS=10, PROPOSED_NEW_NAME='Widgets 🧩/Newer Co'),
    ]

    report_revivals(rows)

    out = capsys.readouterr().out
    assert out.index('Newer Co') < out.index('Older Co')


def test_revivals_reports_the_queued_count_separately_from_the_total(capsys):
    rows = [
        make_row(LABEL='Widgets 🧩/Old/A', VERDICT='REVIVE', ACTION='MOVE', AGE_DAYS=10),
        make_row(LABEL='Widgets 🧩/Old/B', VERDICT='REVIVE', ACTION='SKIP', AGE_DAYS=20,
                 NOTE='BORDERLINE'),
    ]

    report_revivals(rows)

    out = capsys.readouterr().out
    assert '(2, 1 queued)' in out


# ---------------------------- report_fully_stale_branches ----------------------------

def test_no_category_fully_stale_prints_the_none_message(capsys):
    rows = [
        make_row(LABEL='Widgets 🧩/Vendors/Acme Supply', LEAF='yes', VERDICT='ACTIVE'),
    ]

    report_fully_stale_branches(rows)

    assert 'No category came out entirely stale' in capsys.readouterr().out


def test_a_parent_with_every_leaf_stale_is_reported(capsys):
    rows = [
        make_row(LABEL='Widgets 🧩/Vendors/Acme Supply', LEAF='yes', VERDICT='STALE'),
        make_row(LABEL='Widgets 🧩/Vendors/Bramble Ltd', LEAF='yes', VERDICT='STALE'),
    ]

    report_fully_stale_branches(rows)

    out = capsys.readouterr().out
    assert 'Widgets 🧩/Vendors' in out
    assert '2 of 2' in out


def test_a_parent_with_one_active_leaf_is_not_reported(capsys):
    rows = [
        make_row(LABEL='Widgets 🧩/Vendors/Acme Supply', LEAF='yes', VERDICT='STALE'),
        make_row(LABEL='Widgets 🧩/Vendors/Bramble Ltd', LEAF='yes', VERDICT='ACTIVE'),
    ]

    report_fully_stale_branches(rows)

    assert 'No category came out entirely stale' in capsys.readouterr().out


def test_a_parent_with_only_one_leaf_total_is_not_reported(capsys):
    """total[parent] > 1 is required -- a single-child category isn't a
    'branch', it's just one label, and reporting it would be noise."""
    rows = [make_row(LABEL='Widgets 🧩/Vendors/Acme Supply', LEAF='yes', VERDICT='STALE')]

    report_fully_stale_branches(rows)

    assert 'No category came out entirely stale' in capsys.readouterr().out


def test_non_leaf_rows_are_excluded_from_the_branch_count(capsys):
    rows = [
        make_row(LABEL='Widgets 🧩/Vendors', LEAF='no', VERDICT='STALE (has sub-labels)'),
        make_row(LABEL='Widgets 🧩/Vendors/Acme Supply', LEAF='yes', VERDICT='STALE'),
    ]

    report_fully_stale_branches(rows)

    # Only one leaf under the parent -> total[parent] == 1, not reported.
    assert 'No category came out entirely stale' in capsys.readouterr().out


def test_archived_leaves_are_excluded_so_an_old_folder_never_looks_fully_stale(capsys):
    """Without this exclusion, every Old folder's own children (stale by
    construction) would make their parent look like a fully-stale branch."""
    rows = [
        make_row(LABEL='Widgets 🧩/Old/Acme Supply', LEAF='yes', VERDICT='ARCHIVED'),
        make_row(LABEL='Widgets 🧩/Old/Bramble Ltd', LEAF='yes', VERDICT='ARCHIVED'),
    ]

    report_fully_stale_branches(rows)

    assert 'No category came out entirely stale' in capsys.readouterr().out


def test_a_top_level_label_with_no_parent_is_excluded(capsys):
    """A label with no '/' has an empty parent -- there's no branch to
    report a top-level label as part of."""
    rows = [
        make_row(LABEL='Widgets 🧩', LEAF='yes', VERDICT='STALE'),
        make_row(LABEL='Gadgets 🔧', LEAF='yes', VERDICT='STALE'),
    ]

    report_fully_stale_branches(rows)

    assert 'No category came out entirely stale' in capsys.readouterr().out
