"""Covers scripts/hooks/pre-push's actual gating behaviour -- not just that
the installed copy matches the tracked template (test_docs_and_guardrails.py
covers that), but that it genuinely blocks on a failing test/coverage/lint
run or a PII finding, and correctly WARNs rather than blocks when the dev
tooling simply isn't installed. This was previously verified once, manually,
ad hoc, in a live session; this module makes it a real regression suite.

The hook resolves its own repo as two directories up from its own location
(scripts/hooks/pre-push -> repo root), so a copy at that same relative path
inside a throwaway repo is enough to test it in isolation. pytest/coverage/
ruff are stood in for with tiny stub executables in a fake .venv/bin/ whose
exit codes are controlled per test via environment variables, rather than
installing the real tools into a second throwaway venv.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / 'scripts' / 'hooks' / 'pre-push'
CHECK_PII = REPO_ROOT / 'scripts' / 'check-pii.sh'

ZERO = '0' * 40

PYTEST_STUB = '#!/usr/bin/env bash\nexit "${PYTEST_EXIT_CODE:-0}"\n'
COVERAGE_STUB = '''#!/usr/bin/env bash
# Called as either `coverage run -m pytest -q` (simulate the test run via
# PYTEST_EXIT_CODE) or `coverage report` (simulate the floor check via
# COVERAGE_EXIT_CODE) -- the hook invokes both, separately, and checks each.
if [ "$1" = "run" ]; then
  exit "${PYTEST_EXIT_CODE:-0}"
elif [ "$1" = "report" ]; then
  exit "${COVERAGE_EXIT_CODE:-0}"
fi
exit 0
'''
RUFF_STUB = '#!/usr/bin/env bash\nexit "${RUFF_EXIT_CODE:-0}"\n'


def _git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def hook_repo(tmp_path):
    """A throwaway repo carrying copies of the hook and check-pii.sh (the
    hook invokes the latter itself) at their real relative paths, plus one
    clean, committed baseline file."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 't@example.com')
    _git(repo, 'config', 'user.name', 't')

    (repo / 'scripts' / 'hooks').mkdir(parents=True)
    shutil.copy(HOOK, repo / 'scripts' / 'hooks' / 'pre-push')
    (repo / 'scripts' / 'hooks' / 'pre-push').chmod(0o755)
    shutil.copy(CHECK_PII, repo / 'scripts' / 'check-pii.sh')
    (repo / 'scripts' / 'check-pii.sh').chmod(0o755)

    (repo / 'README.md').write_text('hello\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'baseline')
    return repo


def _install_venv_stubs(repo, which):
    """which: subset of {'pytest', 'coverage', 'ruff'} to actually install
    (an absent one exercises the WARN-if-tooling-missing path)."""
    bindir = repo / '.venv' / 'bin'
    bindir.mkdir(parents=True, exist_ok=True)
    stubs = {'pytest': PYTEST_STUB, 'coverage': COVERAGE_STUB, 'ruff': RUFF_STUB}
    for name in which:
        path = bindir / name
        path.write_text(stubs[name])
        path.chmod(0o755)


def _run_hook(repo, env_overrides=None):
    """Feeds the hook a single push line for a brand-new branch (remote_sha
    all-zeros), so RANGE becomes just `local_sha` -- a real, valid range
    check-pii.sh can genuinely scan (everything reachable from HEAD)."""
    local_sha = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    stdin = 'refs/heads/main %s refs/heads/main %s\n' % (local_sha, ZERO)
    env = dict(os.environ)
    env.update(env_overrides or {})
    return subprocess.run(
        ['bash', 'scripts/hooks/pre-push'],
        cwd=repo, input=stdin, capture_output=True, text=True, env=env,
    )


def test_passes_when_everything_is_installed_and_clean(hook_repo):
    _install_venv_stubs(hook_repo, ['pytest', 'coverage', 'ruff'])

    result = _run_hook(hook_repo)

    assert result.returncode == 0


def test_blocks_when_the_pytest_run_fails(hook_repo):
    _install_venv_stubs(hook_repo, ['pytest', 'coverage', 'ruff'])

    result = _run_hook(hook_repo, {'PYTEST_EXIT_CODE': '1'})

    assert result.returncode == 1
    assert 'pytest suite failed' in result.stderr


def test_blocks_when_coverage_drops_below_the_floor(hook_repo):
    _install_venv_stubs(hook_repo, ['pytest', 'coverage', 'ruff'])

    result = _run_hook(hook_repo, {'COVERAGE_EXIT_CODE': '1'})

    assert result.returncode == 1
    assert 'coverage dropped below' in result.stderr


def test_blocks_when_ruff_fails(hook_repo):
    _install_venv_stubs(hook_repo, ['pytest', 'coverage', 'ruff'])

    result = _run_hook(hook_repo, {'RUFF_EXIT_CODE': '1'})

    assert result.returncode == 1
    assert 'ruff check failed' in result.stderr


def test_warns_but_does_not_block_when_no_tooling_is_installed(hook_repo):
    """A fresh checkout that hasn't run `pip install -r requirements-dev.txt`
    yet must not be blocked from pushing at all -- absence of dev tooling is
    expected, not a failure."""
    result = _run_hook(hook_repo)  # no .venv/bin at all

    assert result.returncode == 0
    assert 'pytest not installed' in result.stderr
    assert 'ruff not installed' in result.stderr


def test_runs_bare_pytest_when_coverage_is_missing_but_pytest_is_present(hook_repo):
    _install_venv_stubs(hook_repo, ['pytest', 'ruff'])  # no coverage

    result = _run_hook(hook_repo, {'PYTEST_EXIT_CODE': '1'})

    assert result.returncode == 1
    assert 'pytest suite failed' in result.stderr
    assert 'coverage not installed' in result.stderr


def test_blocks_on_a_real_pii_finding_even_when_all_tooling_passes(hook_repo):
    _install_venv_stubs(hook_repo, ['pytest', 'coverage', 'ruff'])
    # Built via concatenation, not as one literal token, so this test file's
    # own source doesn't self-trigger check-pii.sh when this repo's own
    # history is scanned (see test_check_pii.py for the same convention).
    ssn = '123' + '-45-' + '6789'
    (hook_repo / 'oops.py').write_text('EXAMPLE = "%s"\n' % ssn)
    _git(hook_repo, 'add', '-A')
    _git(hook_repo, 'commit', '-qm', 'accidentally add a real-looking SSN')

    result = _run_hook(hook_repo)

    assert result.returncode == 1
    assert 'check-pii.sh' in result.stderr
