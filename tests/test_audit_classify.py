"""Covers audit.classify -- the verdict/proposal/action decision tree that
used to live inline in audit.main() (176 lines, no seam). This is the module
that makes the staleness and archive/revive rules changeable with
confidence: every VERDICT the code can emit gets its own named test, plus
the branch conditions that choose between them.

classify() is pure: no service, no file I/O, no clock of its own. `now`,
`cutoff`, `stale_days` and `revive_cutoff` are passed in explicitly so these
tests pin the staleness/revival lines instead of racing datetime.now() --
STALE_NOW/STALE_CUTOFF/STALE_DAYS/REVIVE_CUTOFF below reproduce exactly what
audit.main() computes from gc.STALE_YEARS/gc.REVIVE_MONTHS, just fixed in
place.

What this module does NOT cover: reading labels from Gmail, the CSV cache
(test_audit_cache.py), or main()'s row-building/summary/file-writing loop
(test_audit_run.py) -- classify() only decides one label's outcome.
"""

from datetime import timedelta

from conftest import make_cache_entry, utc

import gmail_common as gc
from audit import classify

STALE_NOW = utc(2026, 8, 30)
STALE_CUTOFF = STALE_NOW - timedelta(days=365.2425 * gc.STALE_YEARS)
STALE_DAYS = (STALE_NOW - STALE_CUTOFF).days
REVIVE_CUTOFF = STALE_NOW - timedelta(days=365.2425 * gc.REVIVE_MONTHS / 12)

# Comfortably on either side of the line, clear of BORDERLINE_DAYS (183)
# either way, so tests aimed at ACTIVE/STALE don't accidentally land in the
# borderline band.
RECENT = STALE_NOW - timedelta(days=30)
OLD = STALE_NOW - timedelta(days=STALE_DAYS + 400)

# Older than the revive window but nowhere near the general stale line --
# the case that would have wrongly qualified for REVIVE before this test
# module's threshold was tightened to REVIVE_MONTHS.
OLDER_THAN_REVIVE_WINDOW = REVIVE_CUTOFF - timedelta(days=60)


def _classify(name, all_names, last=None, messages=0):
    entry = make_cache_entry(last=last, messages=messages)
    return classify(name, entry, all_names, STALE_NOW, STALE_CUTOFF, STALE_DAYS,
                     REVIVE_CUTOFF)


# ---------------------------- ARCHIVE CONTAINER ----------------------------

def test_the_old_container_itself_is_never_a_candidate_for_moving():
    result = _classify('Widgets 🧩/Vendors/Old', {'Widgets 🧩/Vendors/Old'})
    assert result['VERDICT'] == 'ARCHIVE CONTAINER'
    assert result['ACTION'] == 'SKIP'
    assert result['PROPOSED_NEW_NAME'] == ''


def test_archive_container_wins_over_has_sub_labels():
    """Precedence: an Old folder that itself has children is still reported
    as the container, not as '... (has sub-labels)'."""
    all_names = {'Widgets 🧩/Vendors/Old', 'Widgets 🧩/Vendors/Old/Acme Supply'}
    result = _classify('Widgets 🧩/Vendors/Old', all_names, last=RECENT)
    assert result['VERDICT'] == 'ARCHIVE CONTAINER'


# ---------------------------- has sub-labels ----------------------------

def test_a_childless_parent_with_no_mail_of_its_own_is_empty_with_sub_labels():
    all_names = {'Widgets 🧩/Vendors', 'Widgets 🧩/Vendors/Acme Supply'}
    result = _classify('Widgets 🧩/Vendors', all_names, last=None)
    assert result['VERDICT'] == 'EMPTY (has sub-labels)'
    assert result['ACTION'] == 'SKIP'
    assert result['PROPOSED_NEW_NAME'] == ''


def test_a_parent_with_active_mail_and_children_is_reported_but_never_moved():
    all_names = {'Widgets 🧩/Vendors', 'Widgets 🧩/Vendors/Acme Supply'}
    result = _classify('Widgets 🧩/Vendors', all_names, last=RECENT)
    assert result['VERDICT'] == 'ACTIVE (has sub-labels)'
    assert result['ACTION'] == 'SKIP'
    assert result['PROPOSED_NEW_NAME'] == ''


def test_a_parent_with_stale_mail_and_children_is_reported_but_never_moved():
    all_names = {'Widgets 🧩/Vendors', 'Widgets 🧩/Vendors/Acme Supply'}
    result = _classify('Widgets 🧩/Vendors', all_names, last=OLD)
    assert result['VERDICT'] == 'STALE (has sub-labels)'
    assert result['ACTION'] == 'SKIP'


def test_an_archived_parent_with_children_is_reported_as_archived_has_sub_labels():
    all_names = {
        'Widgets 🧩/Old',
        'Widgets 🧩/Old/Vendors',
        'Widgets 🧩/Old/Vendors/Acme Supply',
    }
    result = _classify('Widgets 🧩/Old/Vendors', all_names, last=OLD)
    assert result['VERDICT'] == 'ARCHIVED (has sub-labels)'


# ---------------------------- archived leaves ----------------------------

def test_an_archived_label_with_no_mail_is_empty_archived():
    result = _classify('Widgets 🧩/Old/Acme Supply', {'Widgets 🧩/Old/Acme Supply'})
    assert result['VERDICT'] == 'EMPTY (archived)'
    assert result['ACTION'] == 'SKIP'


def test_an_archived_label_still_stale_stays_archived():
    result = _classify('Widgets 🧩/Old/Acme Supply', {'Widgets 🧩/Old/Acme Supply'},
                        last=OLD)
    assert result['VERDICT'] == 'ARCHIVED'
    assert result['ACTION'] == 'SKIP'


def test_an_archived_label_with_fresh_mail_is_queued_to_revive():
    all_names = {'Widgets 🧩/Old/Acme Supply'}
    result = _classify('Widgets 🧩/Old/Acme Supply', all_names, last=RECENT)
    assert result['VERDICT'] == 'REVIVE'
    assert result['ACTION'] == 'MOVE'
    assert result['PROPOSED_NEW_NAME'] == 'Widgets 🧩/Acme Supply'


# classify's REVIVE branch has a defensive `if not proposed:` fallback
# (audit.py) for when revive_name can't find a segment to drop. It is
# unreachable through classify() itself: by the time that branch runs,
# is_archive_container(name) is already False, which means the last segment
# is NOT an archive segment -- so if is_archived(name) is True at all, the
# match has to be on a non-final segment, and archive_segment_index scans
# exactly those. There's no name for which classify reaches this branch and
# revive_name returns None. Not asserted here for that reason; see the
# comment at the fallback's call site in audit.py.


def test_revive_collision_when_the_revived_name_already_exists():
    all_names = {'Widgets 🧩/Old/Acme Supply', 'Widgets 🧩/Acme Supply'}
    result = _classify('Widgets 🧩/Old/Acme Supply', all_names, last=RECENT)
    assert result['VERDICT'] == 'REVIVE'
    assert result['ACTION'] == 'SKIP'
    assert result['COLLISION'] is True
    assert 'already exists' in result['NOTE']


def test_mail_older_than_the_revive_window_stays_archived():
    """The core regression this threshold exists for: a label archived, then
    receiving one email well outside REVIVE_MONTHS but still comfortably
    inside the old 2-year stale window, must NOT be offered for revival --
    only genuinely recent mail should pull it back up a level."""
    all_names = {'Widgets 🧩/Old/Acme Supply'}
    result = _classify('Widgets 🧩/Old/Acme Supply', all_names,
                        last=OLDER_THAN_REVIVE_WINDOW)
    assert result['VERDICT'] == 'ARCHIVED'
    assert result['ACTION'] == 'SKIP'
    assert result['PROPOSED_NEW_NAME'] == ''


def test_mail_exactly_at_the_revive_cutoff_qualifies():
    """Revival is a hard cutoff with no borderline grace zone (unlike the
    stale/active line) -- pin the boundary precisely: mail exactly on the
    line counts as within the window."""
    all_names = {'Widgets 🧩/Old/Acme Supply'}
    result = _classify('Widgets 🧩/Old/Acme Supply', all_names, last=REVIVE_CUTOFF)
    assert result['VERDICT'] == 'REVIVE'
    assert result['ACTION'] == 'MOVE'


def test_mail_one_day_older_than_the_cutoff_does_not_qualify():
    all_names = {'Widgets 🧩/Old/Acme Supply'}
    one_day_older = REVIVE_CUTOFF - timedelta(days=1)
    result = _classify('Widgets 🧩/Old/Acme Supply', all_names, last=one_day_older)
    assert result['VERDICT'] == 'ARCHIVED'
    assert result['ACTION'] == 'SKIP'


# ---------------------------- unarchived leaves ----------------------------

def test_a_label_with_no_mail_at_all_is_empty():
    result = _classify('Widgets 🧩/Vendors/Acme Supply',
                        {'Widgets 🧩/Vendors/Acme Supply'})
    assert result['VERDICT'] == 'EMPTY'
    assert result['ACTION'] == 'SKIP'
    assert result['PROPOSED_NEW_NAME'] == ''


def test_a_label_with_recent_mail_is_active_and_not_proposed():
    result = _classify('Widgets 🧩/Vendors/Acme Supply',
                        {'Widgets 🧩/Vendors/Acme Supply'}, last=RECENT)
    assert result['VERDICT'] == 'ACTIVE'
    assert result['ACTION'] == 'SKIP'
    assert result['PROPOSED_NEW_NAME'] == ''


def test_a_stale_label_is_queued_to_move_under_old():
    all_names = {'Widgets 🧩/Vendors/Acme Supply'}
    result = _classify('Widgets 🧩/Vendors/Acme Supply', all_names, last=OLD)
    assert result['VERDICT'] == 'STALE'
    assert result['ACTION'] == 'MOVE'
    assert result['PROPOSED_NEW_NAME'] == 'Widgets 🧩/Vendors/Old/Acme Supply'


def test_a_stale_label_whose_target_already_exists_is_a_collision():
    all_names = {'Widgets 🧩/Vendors/Acme Supply', 'Widgets 🧩/Vendors/Old/Acme Supply'}
    result = _classify('Widgets 🧩/Vendors/Acme Supply', all_names, last=OLD)
    assert result['VERDICT'] == 'STALE'
    assert result['ACTION'] == 'SKIP'
    assert result['COLLISION'] is True


def test_a_borderline_stale_label_defaults_to_skip_but_keeps_its_proposal():
    all_names = {'Widgets 🧩/Vendors/Acme Supply'}
    borderline_last = STALE_CUTOFF - timedelta(days=1)  # just barely "stale"
    result = _classify('Widgets 🧩/Vendors/Acme Supply', all_names, last=borderline_last)
    assert result['VERDICT'] == 'STALE'
    assert result['ACTION'] == 'SKIP'
    assert result['BORDERLINE'] is True
    assert result['PROPOSED_NEW_NAME'] == 'Widgets 🧩/Vendors/Old/Acme Supply'


def test_a_borderline_active_label_also_defaults_to_skip():
    """Borderline is symmetric: within BORDERLINE_DAYS on the ACTIVE side of
    the line also defaults to SKIP, and still carries a proposal so a human
    can flip ACTION to MOVE without recomputing anything."""
    all_names = {'Widgets 🧩/Vendors/Acme Supply'}
    borderline_last = STALE_CUTOFF + timedelta(days=1)
    result = _classify('Widgets 🧩/Vendors/Acme Supply', all_names, last=borderline_last)
    assert result['VERDICT'] == 'ACTIVE'
    assert result['ACTION'] == 'SKIP'
    assert result['BORDERLINE'] is True
    assert result['PROPOSED_NEW_NAME'] == 'Widgets 🧩/Vendors/Old/Acme Supply'


def test_outside_the_borderline_band_is_not_flagged_borderline():
    all_names = {'Widgets 🧩/Vendors/Acme Supply'}
    just_outside = STALE_CUTOFF - timedelta(days=gc.BORDERLINE_DAYS + 1)
    result = _classify('Widgets 🧩/Vendors/Acme Supply', all_names, last=just_outside)
    assert result['BORDERLINE'] is False
    assert result['ACTION'] == 'MOVE'


# ---------------------------- derived display fields ----------------------------

def test_last_email_and_age_days_are_derived_from_the_cache_entry():
    when = STALE_NOW - timedelta(days=10)
    result = _classify('Widgets 🧩/Vendors/Acme Supply',
                        {'Widgets 🧩/Vendors/Acme Supply'}, last=when, messages=42)
    assert result['LAST_EMAIL'] == when.date().isoformat()
    assert result['AGE_DAYS'] == 10
    assert result['MESSAGES'] == 42


def test_no_mail_leaves_last_email_and_age_days_blank():
    result = _classify('Widgets 🧩/Vendors/Acme Supply',
                        {'Widgets 🧩/Vendors/Acme Supply'}, last=None)
    assert result['LAST_EMAIL'] == ''
    assert result['AGE_DAYS'] == ''


def test_leaf_field_reflects_is_leaf():
    all_names = {'Widgets 🧩/Vendors', 'Widgets 🧩/Vendors/Acme Supply'}
    assert _classify('Widgets 🧩/Vendors', all_names)['LEAF'] == 'no'
    assert _classify('Widgets 🧩/Vendors/Acme Supply', all_names)['LEAF'] == 'yes'
