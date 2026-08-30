"""Covers audit.load_cache and audit.save_cache -- the .audit_cache.json
envelope (version, written timestamp, entries) that lets a re-run skip
re-reading every label from Gmail. Doesn't cover what a cache HIT feeds into
(that's classify()/main()'s job) -- just whether the cache itself is trusted
or discarded.
"""

import json
from datetime import UTC, datetime, timedelta

import gmail_common as gc
from audit import load_cache, save_cache


def _write_cache(path, blob):
    with open(path, 'w') as fh:
        json.dump(blob, fh)


def test_missing_cache_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, 'CACHE_FILE', str(tmp_path / 'nope.json'))
    assert load_cache(refresh=False) == {}


def test_refresh_flag_ignores_an_otherwise_valid_cache(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    _write_cache(cache_file, {
        'version': 1,
        'written': datetime.now(UTC).isoformat(),
        'entries': {'id-1': {'last': None, 'messages': 0}},
    })
    assert load_cache(refresh=True) == {}


def test_unparseable_json_returns_empty(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    cache_file.write_text('{not json')
    assert load_cache(refresh=False) == {}


def test_wrong_version_is_discarded(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    _write_cache(cache_file, {
        'version': 2,
        'written': datetime.now(UTC).isoformat(),
        'entries': {'id-1': {'last': None, 'messages': 0}},
    })
    assert load_cache(refresh=False) == {}


def test_unparseable_written_timestamp_is_discarded(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    _write_cache(cache_file, {
        'version': 1,
        'written': 'not-a-date',
        'entries': {'id-1': {'last': None, 'messages': 0}},
    })
    assert load_cache(refresh=False) == {}


def test_cache_older_than_the_max_age_is_discarded(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    too_old = datetime.now(UTC) - timedelta(days=gc.CACHE_MAX_AGE_DAYS + 1)
    _write_cache(cache_file, {
        'version': 1,
        'written': too_old.isoformat(),
        'entries': {'id-1': {'last': None, 'messages': 0}},
    })
    assert load_cache(refresh=False) == {}


def test_a_fresh_cache_within_the_max_age_is_reused(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    fresh = datetime.now(UTC) - timedelta(days=gc.CACHE_MAX_AGE_DAYS - 1)
    _write_cache(cache_file, {
        'version': 1,
        'written': fresh.isoformat(),
        'entries': {'id-1': {'last': '2026-01-01T00:00:00+00:00', 'messages': 3}},
    })
    assert load_cache(refresh=False) == {
        'id-1': {'last': '2026-01-01T00:00:00+00:00', 'messages': 3},
    }


def test_a_malformed_entry_is_dropped_but_the_rest_of_the_cache_survives(tmp_path, monkeypatch):
    """A cache entry missing 'last' used to raise KeyError deep in main()'s
    per-label loop. It should instead be treated as a cache miss for just
    that one label, re-read fresh, while every other entry stays cached."""
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    _write_cache(cache_file, {
        'version': 1,
        'written': datetime.now(UTC).isoformat(),
        'entries': {
            'id-good': {'last': '2026-01-01T00:00:00+00:00', 'messages': 3},
            'id-missing-last': {'messages': 5},
            'id-not-a-dict': 'oops',
        },
    })
    result = load_cache(refresh=False)
    assert result == {'id-good': {'last': '2026-01-01T00:00:00+00:00', 'messages': 3}}


def test_an_entry_missing_messages_defaults_to_zero_rather_than_dropping(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    _write_cache(cache_file, {
        'version': 1,
        'written': datetime.now(UTC).isoformat(),
        'entries': {'id-1': {'last': None}},
    })
    assert load_cache(refresh=False) == {'id-1': {'last': None, 'messages': 0}}


def test_entries_that_are_not_a_dict_returns_empty(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    _write_cache(cache_file, {
        'version': 1,
        'written': datetime.now(UTC).isoformat(),
        'entries': 'not-a-dict',
    })
    assert load_cache(refresh=False) == {}


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    entries = {'id-1': {'last': '2026-01-01T00:00:00+00:00', 'messages': 7}}

    save_cache(entries)

    assert load_cache(refresh=False) == entries


def test_a_leftover_tmp_file_from_an_interrupted_write_does_not_affect_loading(
        tmp_path, monkeypatch):
    """save_cache() writes to CACHE_FILE + '.tmp' then os.replace()s it over
    the real path -- a crash/Ctrl-C between those two steps can leave a
    stray .tmp file behind, but never a truncated real cache file. Pin that
    load_cache() ignores the leftover .tmp and still sees the last good,
    fully-written cache."""
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    good_entries = {'id-1': {'last': '2026-01-01T00:00:00+00:00', 'messages': 7}}
    save_cache(good_entries)

    # Simulate a write interrupted after the temp file was created but
    # before os.replace() ran -- leave a garbage .tmp file next to the good,
    # already-committed cache.
    (tmp_path / '.audit_cache.json.tmp').write_text('{not valid json, interrupted mid-write')

    assert load_cache(refresh=False) == good_entries
    assert cache_file.exists()
