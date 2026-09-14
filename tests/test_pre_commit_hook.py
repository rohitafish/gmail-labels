"""Covers scripts/hooks/pre-commit -- the layer that stops a real value
before it exists in any commit at all.

The pre-push hook (tests/test_pre_push_hook.py) is the backstop; this one is
the front door. The distinction matters in practice: a value caught at push
time is already in local history and needs a rebase or a reset to remove,
while a value caught here has simply not been committed yet.

Both of its scans run against the INDEX, so every case below stages content
without committing it. gitleaks is stubbed for the same reason as in the
pre-push tests -- it isn't installed on a CI runner, and what is under test
is the hook's gating, not gitleaks' own detection.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / 'scripts' / 'hooks' / 'pre-commit'
CHECK_PII = REPO_ROOT / 'scripts' / 'check-pii.sh'
GITLEAKS_CONFIG = REPO_ROOT / '.gitleaks.toml'

GITLEAKS_STUB = '#!/usr/bin/env bash\nexit "${GITLEAKS_EXIT_CODE:-0}"\n'
PATH_WITHOUT_GITLEAKS = '/usr/bin:/bin:/usr/sbin:/sbin'


# core.hooksPath=/dev/null -- see the note in tests/test_check_pii.py: a
# developer's global hooks fire in these throwaway repos too.
def _git(repo, *args):
    subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args],
                   cwd=repo, check=True, capture_output=True)


@pytest.fixture
def hook_repo(tmp_path):
    """A throwaway repo carrying the hook, check-pii.sh (which the hook
    invokes) and the gitleaks config at their real relative paths -- the
    hook resolves its own repo as two directories up from itself."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 't@example.com')
    _git(repo, 'config', 'user.name', 't')

    (repo / 'scripts' / 'hooks').mkdir(parents=True)
    shutil.copy(HOOK, repo / 'scripts' / 'hooks' / 'pre-commit')
    (repo / 'scripts' / 'hooks' / 'pre-commit').chmod(0o755)
    shutil.copy(CHECK_PII, repo / 'scripts' / 'check-pii.sh')
    (repo / 'scripts' / 'check-pii.sh').chmod(0o755)
    shutil.copy(GITLEAKS_CONFIG, repo / '.gitleaks.toml')

    (repo / '.gitignore').write_text('.pii-denylist\n')
    (repo / 'README.md').write_text('hello\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'baseline')

    bindir = repo / 'fakebin'
    bindir.mkdir()
    stub = bindir / 'gitleaks'
    stub.write_text(GITLEAKS_STUB)
    stub.chmod(0o755)
    return repo


def _run_hook(repo, env_overrides=None):
    env = dict(os.environ)
    env['PATH'] = '%s:%s' % (repo / 'fakebin', os.environ['PATH'])
    env.update(env_overrides or {})
    return subprocess.run(
        ['bash', 'scripts/hooks/pre-commit'],
        cwd=repo, capture_output=True, text=True, env=env, timeout=120,
    )


def _stage(repo, relpath, content):
    (repo / relpath).write_text(content)
    _git(repo, 'add', relpath)


def test_a_clean_index_commits(hook_repo):
    _stage(hook_repo, 'notes.md', 'nothing personal here\n')

    result = _run_hook(hook_repo)

    assert result.returncode == 0, result.stdout + result.stderr


def test_blocks_a_denylisted_value_in_the_index(hook_repo):
    (hook_repo / '.pii-denylist').write_text('brambleworth\n')
    _stage(hook_repo, 'notes.md', 'the brambleworth account\n')

    result = _run_hook(hook_repo)

    assert result.returncode == 1
    assert 'check-pii.sh found PII or a secret' in result.stderr


def test_blocks_a_structural_pattern_in_the_index(hook_repo):
    """No denylist at all -- the structural rules are what catch a value
    nobody has listed yet, and they have to run here too."""
    ssn = '123' + '-45-' + '6789'
    _stage(hook_repo, 'oops.py', 'EXAMPLE = "%s"\n' % ssn)

    result = _run_hook(hook_repo)

    assert result.returncode == 1
    assert 'check-pii.sh found PII or a secret' in result.stderr


def test_ignores_an_unstaged_file(hook_repo):
    """Only the index is what a commit records. A value sitting in the
    working tree is not about to be committed, so it must not block one."""
    (hook_repo / '.pii-denylist').write_text('brambleworth\n')
    (hook_repo / 'scratch.md').write_text('the brambleworth account\n')  # never added

    result = _run_hook(hook_repo)

    assert result.returncode == 0, result.stdout + result.stderr


def test_blocks_when_gitleaks_finds_a_credential(hook_repo):
    _stage(hook_repo, 'notes.md', 'nothing personal here\n')

    result = _run_hook(hook_repo, {'GITLEAKS_EXIT_CODE': '1'})

    assert result.returncode == 1
    assert 'gitleaks found a credential' in result.stderr


def test_blocks_when_gitleaks_is_not_installed(hook_repo):
    """Fails closed, exactly as the pre-push hook does: a scanner that never
    ran reporting nothing is not the same as a clean scan."""
    _stage(hook_repo, 'notes.md', 'nothing personal here\n')

    result = _run_hook(hook_repo, {'PATH': PATH_WITHOUT_GITLEAKS})

    assert result.returncode == 1
    assert 'gitleaks is not installed' in result.stderr
    assert '--no-verify' in result.stderr  # the documented single-commit escape


def test_both_scans_run_even_when_the_first_one_fails(hook_repo):
    """The hook records a failure and carries on rather than exiting at the
    first one, so a commit with two different problems reports both instead
    of surfacing them one re-run at a time."""
    (hook_repo / '.pii-denylist').write_text('brambleworth\n')
    _stage(hook_repo, 'notes.md', 'the brambleworth account\n')

    result = _run_hook(hook_repo, {'GITLEAKS_EXIT_CODE': '1'})

    assert result.returncode == 1
    assert 'check-pii.sh found PII or a secret' in result.stderr
    assert 'gitleaks found a credential' in result.stderr
