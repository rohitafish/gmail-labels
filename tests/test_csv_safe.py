"""Covers gmail_common.csv_safe() -- the CSV/formula-injection mitigation.

SETUP-LOCAL.md tells you to open label_audit.csv (and, for an undo, an
apply_log*.csv) in Excel or Sheets. A cell value starting with =, +, - or @
is read by both as the start of a formula rather than shown as literal
text -- classic CSV/formula injection (CWE-1236). csv_safe() is the
mitigation; test_audit_run.py and test_apply_moves.py cover that it's
actually applied at both CSV-writing call sites, not just that the helper
itself works in isolation.
"""

import pytest

import gmail_common as gc


@pytest.mark.parametrize('leading_char', ['=', '+', '-', '@'])
def test_csv_safe_prefixes_every_formula_leading_character(leading_char):
    value = leading_char + 'cmd|"/c calc"!A1'
    assert gc.csv_safe(value) == "'" + value


def test_csv_safe_leaves_an_ordinary_label_name_unchanged():
    assert gc.csv_safe('Widgets 🧩/Vendors/Acme Supply') == 'Widgets 🧩/Vendors/Acme Supply'


def test_csv_safe_leaves_an_empty_string_unchanged():
    assert gc.csv_safe('') == ''


def test_csv_safe_only_checks_the_leading_character():
    """A trigger character *inside* the string, not at the start, is not a
    formula and shouldn't be touched."""
    assert gc.csv_safe('Dosh 💹/A+B') == 'Dosh 💹/A+B'


@pytest.mark.parametrize('value', [42, None, 10.5, ''])
def test_csv_safe_passes_non_triggering_values_through_unchanged(value):
    assert gc.csv_safe(value) == value


def test_csv_safe_passes_non_string_values_through_even_if_str_would_trigger():
    """Only `str` is ever at risk here -- AGE_DAYS is an int, never
    something csv_safe needs to touch, even incidentally."""
    assert gc.csv_safe(-5) == -5
