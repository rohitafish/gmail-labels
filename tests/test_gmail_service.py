"""Covers the service-facing parts of gmail_common.py: service() (the OAuth
seam), retry() (the backoff wrapper every Gmail call goes through), and
list_user_labels(). The pure path-logic functions in the same module are
covered separately in test_label_paths.py.

No real network call and no real OAuth flow happens anywhere in this file --
Credentials, InstalledAppFlow and build are all monkeypatched at the names
gmail_common imported them under, and gc.HERE/gc.CREDENTIALS_FILE are
redirected into tmp_path so nothing here can touch this machine's real
credentials.json or token files.
"""

import os
import stat

import pytest
from conftest import http_error

import gmail_common as gc


def _perm(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class _FakeCreds:
    def __init__(self, valid=True, expired=False, refresh_token=None):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refreshed = False

    def refresh(self, request):
        self.refreshed = True
        self.valid = True

    def to_json(self):
        return '{"fake": "creds"}'


class _FakeFlow:
    def __init__(self, creds):
        self._creds = creds
        self.ran = False

    def run_local_server(self, port=0):
        self.ran = True
        return self._creds


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """Point gc.HERE/gc.CREDENTIALS_FILE at a scratch dir, and record every
    build() call, for every test in this module."""
    monkeypatch.setattr(gc, 'HERE', str(tmp_path))
    monkeypatch.setattr(gc, 'CREDENTIALS_FILE', str(tmp_path / 'credentials.json'))
    calls = []
    monkeypatch.setattr(gc, 'build', lambda *a, **kw: calls.append(kw) or 'fake-service')
    return {'tmp_path': tmp_path, 'build_calls': calls}


# ---------------------------- service() ----------------------------

def test_missing_credentials_json_exits_with_a_setup_pointer(_sandbox):
    with pytest.raises(SystemExit) as excinfo:
        gc.service(gc.SCOPE_READONLY, 'token_readonly.json')
    assert 'SETUP-LOCAL.md' in str(excinfo.value)


def test_a_valid_stored_token_is_reused_without_the_browser_flow(_sandbox, monkeypatch):
    token_path = _sandbox['tmp_path'] / 'token_readonly.json'
    token_path.write_text('{"stub": "token"}')
    creds = _FakeCreds(valid=True)
    monkeypatch.setattr(gc.Credentials, 'from_authorized_user_file',
                        staticmethod(lambda path, scopes: creds))

    def _fail_if_called(*a, **kw):
        raise AssertionError('should not launch the OAuth flow for a valid token')
    monkeypatch.setattr(gc.InstalledAppFlow, 'from_client_secrets_file',
                        staticmethod(_fail_if_called))

    result = gc.service(gc.SCOPE_READONLY, 'token_readonly.json')

    assert result == 'fake-service'
    assert _sandbox['build_calls'][0]['credentials'] is creds


def test_a_corrupt_token_file_falls_back_to_the_oauth_flow(_sandbox, monkeypatch):
    """Credentials.from_authorized_user_file raising ValueError (malformed
    JSON, wrong shape) must not crash service() -- it should be treated the
    same as no token at all."""
    token_path = _sandbox['tmp_path'] / 'token_readonly.json'
    token_path.write_text('not valid json')
    (_sandbox['tmp_path'] / 'credentials.json').write_text('{"installed": {}}')

    def _raise_value_error(path, scopes):
        raise ValueError('bad token file')
    monkeypatch.setattr(gc.Credentials, 'from_authorized_user_file',
                        staticmethod(_raise_value_error))

    fresh_creds = _FakeCreds(valid=True)
    fake_flow = _FakeFlow(fresh_creds)
    monkeypatch.setattr(gc.InstalledAppFlow, 'from_client_secrets_file',
                        staticmethod(lambda path, scopes: fake_flow))

    result = gc.service(gc.SCOPE_READONLY, 'token_readonly.json')

    assert fake_flow.ran
    assert result == 'fake-service'
    assert token_path.read_text() == '{"fake": "creds"}'


def test_expired_creds_with_a_refresh_token_are_refreshed_not_reauthorised(
        _sandbox, monkeypatch):
    token_path = _sandbox['tmp_path'] / 'token_readonly.json'
    token_path.write_text('{"stub": "token"}')
    creds = _FakeCreds(valid=False, expired=True, refresh_token='rt-1')
    monkeypatch.setattr(gc.Credentials, 'from_authorized_user_file',
                        staticmethod(lambda path, scopes: creds))

    def _fail_if_called(*a, **kw):
        raise AssertionError('should refresh, not re-run the OAuth flow')
    monkeypatch.setattr(gc.InstalledAppFlow, 'from_client_secrets_file',
                        staticmethod(_fail_if_called))

    gc.service(gc.SCOPE_READONLY, 'token_readonly.json')

    assert creds.refreshed


def test_the_new_token_file_and_credentials_file_end_up_owner_only(
        _sandbox, monkeypatch):
    creds_path = _sandbox['tmp_path'] / 'credentials.json'
    creds_path.write_text('{"installed": {}}')
    creds_path.chmod(0o644)  # simulate a freshly downloaded, world-readable file
    token_path = _sandbox['tmp_path'] / 'token_readonly.json'

    def _no_token(path, scopes):
        raise ValueError('no token yet')
    monkeypatch.setattr(gc.Credentials, 'from_authorized_user_file',
                        staticmethod(_no_token))
    fresh_creds = _FakeCreds(valid=True)
    monkeypatch.setattr(gc.InstalledAppFlow, 'from_client_secrets_file',
                        staticmethod(lambda path, scopes: _FakeFlow(fresh_creds)))

    gc.service(gc.SCOPE_READONLY, 'token_readonly.json')

    assert _perm(creds_path) == 0o600
    assert _perm(token_path) == 0o600


# ---------------------------- retry() ----------------------------

def test_retry_returns_on_first_success():
    assert gc.retry(lambda: 'ok') == 'ok'


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
def test_retry_retries_each_documented_transient_status(status):
    attempts = {'n': 0}

    def flaky():
        attempts['n'] += 1
        if attempts['n'] < 3:
            raise http_error(status)
        return 'recovered'

    assert gc.retry(flaky) == 'recovered'
    assert attempts['n'] == 3


def test_retry_reraises_a_non_transient_error_immediately():
    attempts = {'n': 0}

    def always_404():
        attempts['n'] += 1
        raise http_error(404)

    with pytest.raises(gc.HttpError):
        gc.retry(always_404)
    assert attempts['n'] == 1  # no retry attempted for a non-transient status


def test_retry_gives_up_after_the_configured_number_of_tries():
    attempts = {'n': 0}

    def always_429():
        attempts['n'] += 1
        raise http_error(429)

    with pytest.raises(gc.HttpError):
        gc.retry(always_429, tries=3)
    assert attempts['n'] == 3


def test_403_is_not_retried_it_raises_immediately():
    """Pinned as documented behaviour, not silently assumed: a genuine
    permission denial (403) means the credentials/scope will never succeed
    for this resource, unlike a genuinely transient 429/5xx -- so it's
    treated the same as a 404, raising on the first attempt rather than
    burning all `tries` attempts and their full backoff (~30s) before
    surfacing. That used to happen per label, so a broken run over hundreds
    of labels could take a very long time to visibly fail."""
    attempts = {'n': 0}

    def always_403():
        attempts['n'] += 1
        raise http_error(403)

    with pytest.raises(gc.HttpError):
        gc.retry(always_403)  # default tries
    assert attempts['n'] == 1


# ---------------------------- list_user_labels() ----------------------------

def test_list_user_labels_keeps_only_user_type_labels(fake_service):
    fake_service.label_list = [
        {'id': 'id-1', 'name': 'Widgets 🧩', 'type': 'user'},
        {'id': 'INBOX', 'name': 'INBOX', 'type': 'system'},
        {'id': 'id-2', 'name': 'Widgets 🧩/Vendors', 'type': 'user'},
    ]

    result = gc.list_user_labels(fake_service)

    assert result == [
        {'id': 'id-1', 'name': 'Widgets 🧩'},
        {'id': 'id-2', 'name': 'Widgets 🧩/Vendors'},
    ]


def test_the_token_file_is_owner_only_from_creation_not_chmod_after(_sandbox, monkeypatch):
    """With the umask cleared, open(path, 'w') would create a 0666 file and
    only a later chmod would tighten it -- a window in which the refresh
    token is world-readable. _write_private creates it 0600 outright, so
    this passes with no chmod-after at all."""
    old_umask = os.umask(0)
    try:
        (_sandbox['tmp_path'] / 'credentials.json').write_text('{"installed": {}}')
        monkeypatch.setattr(gc.Credentials, 'from_authorized_user_file',
                            staticmethod(lambda p, s: (_ for _ in ()).throw(ValueError('none'))))
        monkeypatch.setattr(gc.InstalledAppFlow, 'from_client_secrets_file',
                            staticmethod(lambda p, s: _FakeFlow(_FakeCreds(valid=True))))
        monkeypatch.setattr(gc.os, 'chmod', lambda *a, **kw: None)  # prove creation mode alone suffices

        gc.service(gc.SCOPE_READONLY, 'token_readonly.json')
    finally:
        os.umask(old_umask)

    assert _perm(_sandbox['tmp_path'] / 'token_readonly.json') == 0o600


def test_credentials_json_is_tightened_even_when_no_oauth_flow_runs(_sandbox, monkeypatch):
    """A valid stored token skips the browser flow entirely -- which is
    where the chmod used to live, so a world-readable credentials.json
    stayed that way on every ordinary run."""
    creds_path = _sandbox['tmp_path'] / 'credentials.json'
    creds_path.write_text('{"installed": {}}')
    creds_path.chmod(0o644)
    (_sandbox['tmp_path'] / 'token_readonly.json').write_text('{"stub": "token"}')
    monkeypatch.setattr(gc.Credentials, 'from_authorized_user_file',
                        staticmethod(lambda path, scopes: _FakeCreds(valid=True)))

    gc.service(gc.SCOPE_READONLY, 'token_readonly.json')

    assert _perm(creds_path) == 0o600


def test_rewriting_an_existing_token_file_tightens_a_loose_mode(_sandbox, monkeypatch):
    token_path = _sandbox['tmp_path'] / 'token_readonly.json'
    token_path.write_text('{"stub": "old"}')
    token_path.chmod(0o644)
    (_sandbox['tmp_path'] / 'credentials.json').write_text('{"installed": {}}')
    creds = _FakeCreds(valid=False, expired=True, refresh_token='rt-1')
    monkeypatch.setattr(gc.Credentials, 'from_authorized_user_file',
                        staticmethod(lambda path, scopes: creds))

    gc.service(gc.SCOPE_READONLY, 'token_readonly.json')

    assert token_path.read_text() == '{"fake": "creds"}'
    assert _perm(token_path) == 0o600
