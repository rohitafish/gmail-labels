"""Covers the pure label-path logic in gmail_common.py: in_scope, is_archived,
is_leaf, archive_container, propose_name, is_archive_container,
archive_segment_index and revive_name. None of these touch the network, the
filesystem or the clock -- they're the highest-value, lowest-cost tests in
this codebase, and the ones that make changing the staleness/archive rules
safe. The service-backed functions in gmail_common (service, retry,
list_user_labels) are covered separately in test_gmail_service.py.

Every label name below is fabricated (the "Widgets"/"Acme Supply" shapes),
per CONTRIBUTING.md's rule against ever using a real household detail as an
example, but keeps the emoji-plus-variation-selector shape real label names
have, since that shape is exactly what in_scope's plain startswith match and
the ARCHIVE_SEGMENTS exact-segment match have to survive.
"""

import pytest

import gmail_common as gc

# A representative universe of names for functions that take `all_names`.
# No existing Old/zOld container anywhere in it, so archive_container always
# has to propose a fresh one unless a test adds one.
ALL_NAMES = {
    'Widgets 🧩',
    'Widgets 🧩/Vendors',
    'Widgets 🧩/Vendors/Acme Supply',
    'Widgets 🧩/Vendors/Bramble Ltd',
}


# ---------------------------- in_scope ----------------------------

def test_empty_scope_prefixes_matches_everything(monkeypatch):
    monkeypatch.setattr(gc, 'SCOPE_PREFIXES', [])
    assert gc.in_scope('Anything 🎈/At/All')


def test_only_names_starting_with_a_configured_prefix_are_in_scope(monkeypatch):
    monkeypatch.setattr(gc, 'SCOPE_PREFIXES', ['Widgets'])
    assert gc.in_scope('Widgets 🧩/Vendors')
    assert not gc.in_scope('Gadgets 🔧/Vendors')


def test_in_scope_is_a_plain_prefix_match_not_a_segment_match(monkeypatch):
    """Documented in gmail_common.py: startswith, so the emoji and its
    invisible variation selectors never have to be typed in SCOPE_PREFIXES."""
    monkeypatch.setattr(gc, 'SCOPE_PREFIXES', ['Widgets'])
    assert gc.in_scope('Widgets 🧩')          # prefix alone
    assert gc.in_scope('WidgetsFoo')          # startswith, no separator required


# ---------------------------- is_archived ----------------------------

@pytest.mark.parametrize('name', [
    'Widgets 🧩/Old/Acme Supply',
    'Widgets 🧩/OLD/Acme Supply',       # case-insensitive
    'Widgets 🧩/ Old /Acme Supply',     # whitespace around the segment
    'Widgets 🧩/zOld/Acme Supply',
    'Widgets 🧩/ZOLD/Acme Supply',
])
def test_is_archived_true_when_any_segment_is_an_archive_segment(name):
    assert gc.is_archived(name)


@pytest.mark.parametrize('name', [
    'Widgets 🧩/Vendors/Acme Supply',
    'Widgets 🧩/Oldish/Acme Supply',    # substring of 'old', not the segment itself
    'Widgets 🧩/Golden/Acme Supply',    # contains "old" as a substring
])
def test_is_archived_requires_an_exact_segment_not_a_substring(name):
    assert not gc.is_archived(name)


def test_top_level_zold_with_an_emoji_is_not_recognised_as_archived(monkeypatch):
    """The documented SETUP-LOCAL.md gotcha: with SCOPE_PREFIXES == [], a
    top-level 'zOld 💾' is NOT recognised as an archive, because the segment
    test matches 'zold' exactly and this name carries an emoji. Surprising
    and documented -- pin it so a future change to ARCHIVE_SEGMENTS is a
    visible decision, not a silent behaviour change."""
    assert not gc.is_archived('zOld 💾')
    assert not gc.is_archive_container('zOld 💾')


# ---------------------------- is_leaf ----------------------------

def test_a_label_with_children_is_not_a_leaf():
    assert not gc.is_leaf('Widgets 🧩/Vendors', ALL_NAMES)


def test_a_label_with_no_children_is_a_leaf():
    assert gc.is_leaf('Widgets 🧩/Vendors/Acme Supply', ALL_NAMES)


def test_is_leaf_does_not_match_a_sibling_with_a_shared_prefix():
    """'Widgets 🧩/Vendors/Acme' must not be considered a parent of
    'Widgets 🧩/Vendors/Acme Supply' -- is_leaf requires the '/' separator,
    not a bare string prefix."""
    all_names = ALL_NAMES | {'Widgets 🧩/Vendors/Acme'}
    assert gc.is_leaf('Widgets 🧩/Vendors/Acme', all_names)


# ---------------------------- archive_container ----------------------------

def test_archive_container_proposes_old_when_nothing_exists_yet():
    path, exists = gc.archive_container('Widgets 🧩/Vendors/Acme Supply', ALL_NAMES)
    assert (path, exists) == ('Widgets 🧩/Vendors/Old', False)


def test_archive_container_reuses_an_existing_old():
    all_names = ALL_NAMES | {'Widgets 🧩/Vendors/Old'}
    path, exists = gc.archive_container('Widgets 🧩/Vendors/Acme Supply', all_names)
    assert (path, exists) == ('Widgets 🧩/Vendors/Old', True)


def test_archive_container_reuses_an_existing_zold_when_no_old_exists():
    all_names = ALL_NAMES | {'Widgets 🧩/Vendors/zOld'}
    path, exists = gc.archive_container('Widgets 🧩/Vendors/Acme Supply', all_names)
    assert (path, exists) == ('Widgets 🧩/Vendors/zOld', True)


def test_archive_container_prefers_old_over_zold_when_both_exist():
    all_names = ALL_NAMES | {'Widgets 🧩/Vendors/Old', 'Widgets 🧩/Vendors/zOld'}
    path, exists = gc.archive_container('Widgets 🧩/Vendors/Acme Supply', all_names)
    assert (path, exists) == ('Widgets 🧩/Vendors/Old', True)


def test_archive_container_at_top_level_has_no_parent_segment():
    path, exists = gc.archive_container('Widgets 🧩', {'Widgets 🧩'})
    assert (path, exists) == ('Old', False)


# ---------------------------- propose_name ----------------------------

def test_propose_name_inserts_archive_segment_before_the_final_segment():
    assert (gc.propose_name('Widgets 🧩/Vendors/Acme Supply', ALL_NAMES)
            == 'Widgets 🧩/Vendors/Old/Acme Supply')


def test_propose_name_for_a_top_level_label_files_it_under_a_bare_old():
    assert gc.propose_name('Widgets 🧩', {'Widgets 🧩'}) == 'Old/Widgets 🧩'


# ---------------------------- is_archive_container ----------------------------

@pytest.mark.parametrize('name', [
    'Widgets 🧩/Vendors/Old',
    'Widgets 🧩/Vendors/OLD',
    'Widgets 🧩/Vendors/ Old ',
    'Widgets 🧩/Vendors/zOld',
])
def test_is_archive_container_true_for_the_container_itself(name):
    assert gc.is_archive_container(name)


@pytest.mark.parametrize('name', [
    'Widgets 🧩/Vendors/Older',          # not an exact match
    'Widgets 🧩/Old/Acme Supply',         # Old is a middle segment, not the leaf
])
def test_is_archive_container_false_when_old_is_not_the_final_segment(name):
    assert not gc.is_archive_container(name)


# ---------------------------- archive_segment_index / revive_name ----------------------------

def test_archive_segment_index_ignores_a_trailing_old():
    """A trailing Old is the container itself (is_archive_container's job),
    not something with an archive segment to drop."""
    assert gc.archive_segment_index('Widgets 🧩/Vendors/Old') is None


def test_archive_segment_index_finds_the_segment_nearest_the_leaf():
    assert gc.archive_segment_index('Widgets 🧩/Old/Vendors/Acme Supply') == 1


def test_revive_name_drops_the_archive_segment_nearest_the_leaf():
    assert (gc.revive_name('Widgets 🧩/Vendors/Old/Acme Supply')
            == 'Widgets 🧩/Vendors/Acme Supply')


def test_revive_name_deep_example_matches_setup_local_docs():
    """SETUP-LOCAL.md's own worked example, with fabricated names in the
    same shape."""
    assert (gc.revive_name('Widgets 🧩/Old/Vendors/Acme Supply')
            == 'Widgets 🧩/Vendors/Acme Supply')


def test_revive_name_returns_none_with_no_archive_segment_to_drop():
    assert gc.revive_name('Widgets 🧩/Vendors/Acme Supply') is None


def test_revive_name_on_the_container_itself_returns_none():
    assert gc.revive_name('Widgets 🧩/Vendors/Old') is None


# ---------------------------- the round-trip invariant ----------------------------

# SETUP-LOCAL.md:136 claims propose_name and revive_name are "exact inverses
# -- verified against all 228 real moves from the first run." That
# verification happened once, by hand, against one mailbox, and nothing
# since has re-checked it. This table is the standing replacement.
ROUND_TRIP_NAMES = [
    'Widgets 🧩',
    'Widgets 🧩/Vendors/Acme Supply',
    'Widgets 🧩/Vendors/Bramble Ltd',
    'Hobbies 🎲/Chess',
    'Computing 🖥/Vendor/Reseller',
]


@pytest.mark.parametrize('name', ROUND_TRIP_NAMES)
def test_propose_then_revive_is_the_identity(name):
    all_names = set(ROUND_TRIP_NAMES)
    proposed = gc.propose_name(name, all_names)
    assert gc.revive_name(proposed) == name


def test_the_round_trip_table_actually_has_cases_in_it():
    """Anti-vacuity guard: if ROUND_TRIP_NAMES were ever emptied by a
    refactor, the parametrised test above would vacuously pass with zero
    cases and this invariant would go silently unverified again."""
    assert len(ROUND_TRIP_NAMES) >= 5
