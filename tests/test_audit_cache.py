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


def _bare(entries):
    """Loaded entries without 'fetched', for tests that aren't about age."""
    return {label_id: {k: v for k, v in entry.items() if k != 'fetched'}
            for label_id, entry in entries.items()}


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
        'id-1': {'last': '2026-01-01T00:00:00+00:00', 'messages': 3,
                 'fetched': fresh.isoformat()},     # falls back to 'written'
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
    result = _bare(load_cache(refresh=False))
    assert result == {'id-good': {'last': '2026-01-01T00:00:00+00:00', 'messages': 3}}


def test_an_entry_missing_messages_defaults_to_zero_rather_than_dropping(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    _write_cache(cache_file, {
        'version': 1,
        'written': datetime.now(UTC).isoformat(),
        'entries': {'id-1': {'last': None}},
    })
    assert _bare(load_cache(refresh=False)) == {'id-1': {'last': None, 'messages': 0}}


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

    assert _bare(load_cache(refresh=False)) == entries


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

    assert _bare(load_cache(refresh=False)) == good_entries
    assert cache_file.exists()


# ---------------------------- per-entry age ----------------------------
# 'written' is refreshed by every run, so it cannot be what ages an entry:
# a date read on day 0 and reused on day 6 would be re-stamped day 6 and
# live another week, for as long as the audit ran at least weekly.

def test_an_entry_fetched_too_long_ago_is_dropped_even_in_a_freshly_written_cache(
        tmp_path, monkeypatch, capsys):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    now = datetime.now(UTC)
    old = (now - timedelta(days=gc.CACHE_MAX_AGE_DAYS + 1)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    _write_cache(cache_file, {
        'version': 1,
        'written': now.isoformat(),
        'entries': {
            'id-old': {'last': None, 'messages': 0, 'fetched': old},
            'id-recent': {'last': None, 'messages': 0, 'fetched': recent},
        },
    })

    assert load_cache(refresh=False) == {
        'id-recent': {'last': None, 'messages': 0, 'fetched': recent},
    }
    assert '1 cached date(s) are over' in capsys.readouterr().out


def test_an_unusable_fetched_time_falls_back_to_the_written_time(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    written = datetime.now(UTC) - timedelta(days=2)
    _write_cache(cache_file, {
        'version': 1,
        'written': written.isoformat(),
        'entries': {
            'id-garbage': {'last': None, 'fetched': 'not-a-date'},
            'id-naive': {'last': None, 'fetched': '2026-01-01T00:00:00'},  # no timezone
        },
    })

    result = load_cache(refresh=False)

    assert {entry['fetched'] for entry in result.values()} == {written.isoformat()}


def test_a_timezone_less_written_time_is_discarded(tmp_path, monkeypatch):
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    _write_cache(cache_file, {
        'version': 1,
        'written': '2026-01-01T00:00:00',
        'entries': {'id-1': {'last': None, 'messages': 0}},
    })
    assert load_cache(refresh=False) == {}


def test_reloading_and_saving_does_not_make_an_entry_any_younger(tmp_path, monkeypatch):
    """The regression itself: load, save, load again -- the entry must still
    carry the time it was actually read from Gmail."""
    cache_file = tmp_path / '.audit_cache.json'
    monkeypatch.setattr(gc, 'CACHE_FILE', str(cache_file))
    fetched = (datetime.now(UTC) - timedelta(days=gc.CACHE_MAX_AGE_DAYS - 1)).isoformat()
    _write_cache(cache_file, {
        'version': 1,
        'written': fetched,
        'entries': {'id-1': {'last': None, 'messages': 0, 'fetched': fetched}},
    })

    save_cache(load_cache(refresh=False))

    assert load_cache(refresh=False)['id-1']['fetched'] == fetched
