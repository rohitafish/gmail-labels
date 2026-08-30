"""Shared fixtures for the test suite.

No mocking library is used anywhere in this suite -- monkeypatch plus a
small hand-rolled fake Gmail service, matching the style described in
CONTRIBUTING.md. The fake backs the five calls the codebase actually makes
(labels().list/create/patch, messages().list/get), all reached the same
way production reaches them: through gc.retry(lambda: ...execute()).

`sys.path.insert` + `os.chdir` at collection time mirror what the running
scripts assume -- gmail_common.HERE resolves relative to the module's own
file, but audit.py/apply_moves.py's --out/--csv defaults and this suite's
own tmp_path fixtures are simplest to reason about from the repo root.
"""

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httplib2
import pytest
from googleapiclient.errors import HttpError

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import gmail_common as gc  # noqa: E402


def http_error(status, message=b'error'):
    """A real googleapiclient HttpError with a given .resp.status.

    httplib2.Response is the actual response shape the library raises with
    in production (a bare object with a `.status` attribute isn't enough --
    HttpError.__init__ also reads `.reason` off it, which only a real
    httplib2.Response provides out of the box).
    """
    return HttpError(httplib2.Response({'status': status}), message)


class _Call:
    """One in-flight fluent-chain call, e.g. `svc.users().labels().list(...)`.

    Built by a FakeGmailService resource method; only comes alive on
    `.execute()`, exactly like the real googleapiclient resource objects --
    gc.retry() re-invokes the whole `lambda: svc...execute()` chain on each
    retry attempt, so recording happens here, at execute() time, not at
    construction time.
    """

    def __init__(self, service, name, fn):
        self._service = service
        self._name = name
        self._fn = fn

    def execute(self):
        self._service.calls.append(self._name)
        queue = self._service.errors.get(self._name)
        if queue:
            err = queue.pop(0)
            if err is not None:
                raise err
        return self._fn()


class _FakeLabelsResource:
    def __init__(self, service):
        self._service = service

    def list(self, userId='me'):
        return _Call(self._service, 'labels.list',
                     lambda: {'labels': list(self._service.label_list)})

    def create(self, userId='me', body=None):
        return _Call(self._service, 'labels.create',
                     lambda: self._service._create_label(body))

    def patch(self, userId='me', id=None, body=None):
        return _Call(self._service, 'labels.patch',
                     lambda: self._service._patch_label(id, body))


class _FakeMessagesResource:
    def __init__(self, service):
        self._service = service

    def list(self, userId='me', labelIds=None, maxResults=None):
        label_id = labelIds[0]
        return _Call(self._service, 'messages.list',
                     lambda: self._service._list_messages(label_id))

    def get(self, userId='me', id=None, format=None):
        return _Call(self._service, 'messages.get',
                     lambda: self._service._get_message(id))


class FakeGmailService:
    """A hand-rolled stand-in for the googleapiclient Gmail resource.

    `label_list` is a list of {'id', 'name', 'type'} dicts -- the same shape
    gc.list_user_labels reads. Named `label_list`, not `labels`, because
    `labels()` is itself a method here (matching the real fluent-chain API,
    `svc.users().labels()...`) -- the two would otherwise collide, the data
    attribute silently shadowing the method on the same instance. `mail`
    maps label id -> (datetime_or_None, message_count): the newest message
    on that label and the estimated total, i.e. exactly what
    audit.last_message returns. Every executed call is recorded in `.calls`
    (as 'labels.list', 'messages.get', etc.) so a test can assert what was
    actually sent, including call counts -- e.g. that a cache hit issues
    zero messages.* calls.
    """

    def __init__(self, labels=None, mail=None):
        self.label_list = list(labels or [])
        self.mail = dict(mail or {})
        self.calls = []
        self.errors = {}
        self._next_id = 1000

    def queue_error(self, call_name, err, times=1):
        """Make the next `times` executions of `call_name` raise `err`."""
        self.errors.setdefault(call_name, [])
        self.errors[call_name].extend([err] * times)

    def users(self):
        return self

    def labels(self):
        return _FakeLabelsResource(self)

    def messages(self):
        return _FakeMessagesResource(self)

    def _create_label(self, body):
        new_id = 'FAKE_LABEL_%d' % self._next_id
        self._next_id += 1
        entry = {'id': new_id, 'name': body['name'], 'type': 'user'}
        self.label_list.append(entry)
        return entry

    def _patch_label(self, label_id, body):
        for label in self.label_list:
            if label['id'] == label_id:
                label['name'] = body['name']
                return label
        raise KeyError('no such label id: %s' % label_id)

    def _list_messages(self, label_id):
        when, estimate = self.mail.get(label_id, (None, 0))
        if when is None:
            return {'messages': [], 'resultSizeEstimate': estimate}
        return {'messages': [{'id': label_id + '-msg'}], 'resultSizeEstimate': estimate}

    def _get_message(self, message_id):
        label_id = message_id.rsplit('-msg', 1)[0]
        when, _estimate = self.mail[label_id]
        return {'internalDate': str(int(when.timestamp() * 1000))}


@pytest.fixture()
def fake_service():
    return FakeGmailService()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Stub out every time.sleep this codebase calls, in every module that
    imports it -- without this, the retry-backoff tests alone (six attempts,
    exponential) take real wall-clock seconds, and audit's per-lookup sleep
    and apply_moves' per-rename sleep would slow the whole suite for no
    reason a test cares about. A test that DOES care about pacing asserts on
    a recorded list of sleep durations instead (see test_gmail_service.py's
    retry tests), which this still allows -- it replaces the sleep, it
    doesn't hide that one was requested.
    """
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)


@pytest.fixture(autouse=True)
def _isolated_cache_file(tmp_path, monkeypatch):
    """Every test gets its own .audit_cache.json under tmp_path, never this
    repo's real one at gc.CACHE_FILE's default location -- without this, any
    test that calls audit.main()/load_cache/save_cache without its own
    override would silently read and write this actual project directory's
    cache file. A test that wants specific cache content still points
    gc.CACHE_FILE at a path of its own choosing; that later monkeypatch call
    simply overrides this one.
    """
    monkeypatch.setattr(gc, 'CACHE_FILE', str(tmp_path / '.audit_cache.json'))


def utc(*args, **kwargs):
    """Shorthand for a UTC-aware datetime, since every date this codebase
    compares (internalDate, cache entries, `now`) is UTC."""
    return datetime(*args, tzinfo=UTC, **kwargs)


def make_label(name, label_id=None):
    """A label dict shaped like gc.list_user_labels' output."""
    return {'id': label_id or ('id-' + name), 'name': name, 'type': 'user'}


def make_cache_entry(last=None, messages=0):
    """One .audit_cache.json entry: {'last': iso_or_None, 'messages': int}."""
    return {'last': last.isoformat() if last else None, 'messages': messages}


def make_row(**overrides):
    """A CSV_COLUMNS-shaped audit row, for tests of the reporting functions
    that consume already-classified rows rather than raw labels."""
    row = {
        'LABEL': 'Widgets 🧩/Vendors/Acme Supply',
        'LABEL_ID': 'id-1',
        'LAST_EMAIL': '',
        'AGE_DAYS': '',
        'MESSAGES': 0,
        'LEAF': 'yes',
        'VERDICT': 'ACTIVE',
        'PROPOSED_NEW_NAME': '',
        'ACTION': 'SKIP',
        'NOTE': '',
    }
    row.update(overrides)
    return row
