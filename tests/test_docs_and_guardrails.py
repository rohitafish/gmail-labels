"""Meta-tests: assert the documentation and the installed guardrail match
the code, not just that the code works in isolation. SETUP-LOCAL.md is the
operating manual a human actually follows -- a verdict classify() can emit
but SETUP-LOCAL.md doesn't document, or a tuning value the docs quote that
no longer matches gmail_common.py, is a real failure mode for a tool whose
whole interface is "read the CSV, edit ACTION, re-run".

Each check below fails with a message telling you to update the doc (or
re-install the hook), not to delete the assertion -- these exist to catch
exactly that drift.
"""

import ast
import filecmp
import re
from datetime import timedelta
from pathlib import Path

from conftest import utc

import gmail_common as gc
from audit import classify

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_LOCAL = (REPO_ROOT / 'SETUP-LOCAL.md').read_text(encoding='utf-8')
CONFIG_REFERENCE = (REPO_ROOT / 'wiki-drafts' / 'Configuration-Reference.md').read_text(encoding='utf-8')


def _section(text, heading, next_heading_prefix='#'):
    """The text between a heading line and the next line starting with
    next_heading_prefix (or end of file)."""
    start = text.index(heading) + len(heading)
    rest = text[start:]
    lines = rest.splitlines()
    body = []
    for line in lines[1:]:  # skip the heading's own line remainder
        if line.startswith(next_heading_prefix):
            break
        body.append(line)
    return '\n'.join(body)


# ---------------------------- every VERDICT classify() can emit is documented ----------------------------

# Every distinct base verdict shape classify() can return. The four
# "... (has sub-labels)" variants are covered by the doc's single generic
# '… (has sub-labels)' row, not four separate literal rows -- checked
# separately below.
ALL_CLASSIFY_VERDICTS = [
    'ACTIVE', 'STALE', 'EMPTY', 'ARCHIVED', 'REVIVE', 'ARCHIVE CONTAINER',
    'EMPTY (archived)',
]
HAS_SUB_LABEL_VERDICTS = [
    'ACTIVE (has sub-labels)', 'STALE (has sub-labels)',
    'ARCHIVED (has sub-labels)', 'EMPTY (has sub-labels)',
]


def test_the_verdict_lists_above_actually_have_cases_in_them():
    """Anti-vacuity guard: if either list were emptied by a refactor, the
    tests below would vacuously pass having checked nothing."""
    assert len(ALL_CLASSIFY_VERDICTS) >= 7
    assert len(HAS_SUB_LABEL_VERDICTS) == 4


def test_every_base_verdict_is_documented_in_the_verdicts_table():
    verdict_table = _section(SETUP_LOCAL, '### Verdicts', '\n---')
    documented = set(re.findall(r'^\|\s*\*{0,2}`([^`]+)`', verdict_table, re.MULTILINE))

    for verdict in ALL_CLASSIFY_VERDICTS:
        assert verdict in documented, (
            f"classify() can return VERDICT={verdict!r} but SETUP-LOCAL.md's "
            "Verdicts table doesn't mention it. Add a row documenting it "
            "rather than deleting this assertion."
        )


def test_the_has_sub_labels_variants_are_covered_by_the_generic_row():
    verdict_table = _section(SETUP_LOCAL, '### Verdicts', '\n---')
    assert '(has sub-labels)' in verdict_table, (
        "None of the '... (has sub-labels)' verdicts classify() can return "
        "are documented -- the generic row covering all four is missing."
    )


def test_classify_does_not_emit_a_verdict_shape_the_documentation_lists_have_not_anticipated():
    """The inverse check: run classify() over enough varied inputs to
    surface every verdict shape it actually produces, and confirm none of
    them are new/unknown relative to the two lists above -- a change to the
    decision tree that introduces a brand-new VERDICT string should fail
    here, not go unnoticed."""
    now = utc(2026, 8, 30)
    cutoff = now - timedelta(days=365.2425 * gc.STALE_YEARS)
    stale_days = (now - cutoff).days
    revive_cutoff = now - timedelta(days=365.2425 * gc.REVIVE_MONTHS / 12)
    recent = now - timedelta(days=10)
    old = now - timedelta(days=stale_days + 400)

    cases = [
        ('Widgets 🧩/Vendors/Old', {'Widgets 🧩/Vendors/Old'}, None),
        ('Widgets 🧩/Vendors', {'Widgets 🧩/Vendors', 'Widgets 🧩/Vendors/Acme Supply'}, None),
        ('Widgets 🧩/Vendors', {'Widgets 🧩/Vendors', 'Widgets 🧩/Vendors/Acme Supply'}, recent),
        ('Widgets 🧩/Vendors', {'Widgets 🧩/Vendors', 'Widgets 🧩/Vendors/Acme Supply'}, old),
        ('Widgets 🧩/Old/Acme Supply', {'Widgets 🧩/Old/Acme Supply'}, None),
        ('Widgets 🧩/Old/Acme Supply', {'Widgets 🧩/Old/Acme Supply'}, old),
        ('Widgets 🧩/Old/Acme Supply', {'Widgets 🧩/Old/Acme Supply'}, recent),
        ('Widgets 🧩/Vendors/Acme Supply', {'Widgets 🧩/Vendors/Acme Supply'}, None),
        ('Widgets 🧩/Vendors/Acme Supply', {'Widgets 🧩/Vendors/Acme Supply'}, recent),
        ('Widgets 🧩/Vendors/Acme Supply', {'Widgets 🧩/Vendors/Acme Supply'}, old),
    ]
    seen = set()
    for name, all_names, last in cases:
        entry = {'last': last.isoformat() if last else None, 'messages': 0}
        result = classify(name, entry, all_names, now, cutoff, stale_days, revive_cutoff)
        seen.add(result['VERDICT'])

    known = set(ALL_CLASSIFY_VERDICTS) | set(HAS_SUB_LABEL_VERDICTS)
    unknown = seen - known
    assert not unknown, (
        f"classify() produced verdict(s) {unknown} that this test file doesn't "
        "know about -- add them to ALL_CLASSIFY_VERDICTS/HAS_SUB_LABEL_VERDICTS "
        "above and to SETUP-LOCAL.md's Verdicts table."
    )


# ---------------------------- CSV column table matches CSV_COLUMNS ----------------------------

def test_the_csv_column_table_matches_csv_columns_in_order():
    column_table = _section(SETUP_LOCAL, '### The CSV', '\n---')
    documented = re.findall(r'^\|\s*\*{0,2}([A-Z_]+)\*{0,2}\s*\|', column_table, re.MULTILINE)
    assert documented == gc.CSV_COLUMNS, (
        "SETUP-LOCAL.md's CSV column table doesn't match gc.CSV_COLUMNS "
        f"(doc: {documented}, code: {gc.CSV_COLUMNS}) -- update whichever "
        "one is out of date, don't just widen this assertion."
    )


# ---------------------------- tuning block quotes live values ----------------------------

def test_the_tuning_block_quotes_the_live_config_values():
    tuning = _section(SETUP_LOCAL, '## Tuning', '\n---')

    for name, live_value in (
        ('SCOPE_PREFIXES', gc.SCOPE_PREFIXES),
        ('STALE_YEARS', gc.STALE_YEARS),
        ('BORDERLINE_DAYS', gc.BORDERLINE_DAYS),
        ('REVIVE_MONTHS', gc.REVIVE_MONTHS),
    ):
        match = re.search(r'^%s\s*=\s*(.+)$' % name, tuning, re.MULTILINE)
        assert match, f"SETUP-LOCAL.md's Tuning section no longer quotes {name}"
        documented_value = ast.literal_eval(match.group(1).strip())
        assert documented_value == live_value, (
            f"SETUP-LOCAL.md says {name} = {documented_value!r}, but "
            f"gmail_common.py's real value is {live_value!r} -- the tuning "
            "block has drifted from the actual config."
        )


# ---------------------------- wiki's Configuration Reference matches live constants ----------------------------

def test_configuration_reference_wiki_page_quotes_the_live_config_values():
    """SETUP-LOCAL.md's tuning block has its own drift test above --
    wiki-drafts/Configuration-Reference.md repeats the same constants in a
    fuller table and had no equivalent, so it could silently drift the next
    time one of these changed. Every value in that table happens to be a
    valid Python literal (a list, a tuple, a string, an int), so the same
    ast.literal_eval approach works unchanged."""
    for name, live_value in (
        ('SCOPE_PREFIXES', gc.SCOPE_PREFIXES),
        ('STALE_YEARS', gc.STALE_YEARS),
        ('BORDERLINE_DAYS', gc.BORDERLINE_DAYS),
        ('REVIVE_MONTHS', gc.REVIVE_MONTHS),
        ('OLD_NAME', gc.OLD_NAME),
        ('ARCHIVE_SEGMENTS', gc.ARCHIVE_SEGMENTS),
        ('CACHE_MAX_AGE_DAYS', gc.CACHE_MAX_AGE_DAYS),
    ):
        match = re.search(r'^\|\s*`%s`\s*\|\s*`([^`]+)`' % re.escape(name),
                          CONFIG_REFERENCE, re.MULTILINE)
        assert match, (
            f"wiki-drafts/Configuration-Reference.md's table no longer has a "
            f"row for {name} -- add one rather than deleting this assertion."
        )
        documented_value = ast.literal_eval(match.group(1).strip())
        assert documented_value == live_value, (
            f"wiki-drafts/Configuration-Reference.md says {name} = "
            f"{documented_value!r}, but gmail_common.py's real value is "
            f"{live_value!r} -- the wiki page has drifted from the actual "
            "config."
        )


def test_every_gmail_common_config_constant_has_a_configuration_reference_row():
    """The inverse check: a brand-new tunable constant added to
    gmail_common.py's CONFIG block should fail here, not silently go
    undocumented on the wiki page."""
    documented_names = set(re.findall(r'^\|\s*`([A-Z_]+)`', CONFIG_REFERENCE, re.MULTILINE))
    live_names = {'SCOPE_PREFIXES', 'STALE_YEARS', 'BORDERLINE_DAYS', 'REVIVE_MONTHS',
                  'OLD_NAME', 'ARCHIVE_SEGMENTS', 'CACHE_MAX_AGE_DAYS'}
    assert live_names <= documented_names, (
        f"{live_names - documented_names} missing from "
        "wiki-drafts/Configuration-Reference.md's table"
    )


# ---------------------------- installed pre-push hook matches the tracked template ----------------------------

def test_installed_pre_push_hook_matches_the_tracked_template():
    installed = REPO_ROOT / '.git' / 'hooks' / 'pre-push'
    template = REPO_ROOT / 'scripts' / 'hooks' / 'pre-push'

    if not installed.exists():
        import pytest
        pytest.skip('pre-push hook not installed on this machine (see SETUP-LOCAL.md)')

    assert filecmp.cmp(installed, template, shallow=False), (
        ".git/hooks/pre-push has drifted from scripts/hooks/pre-push -- "
        "re-copy it: cp scripts/hooks/pre-push .git/hooks/pre-push"
    )
