"""Covers scripts/publish-wiki.sh -- the script that publishes wiki-drafts/
to the live, public GitHub wiki. It exists specifically because a sibling
project's manual wiki publish once leaked the operator's real name and
hostname into a public commit (a throwaway `git clone` of a wiki repo has
no git identity of its own and silently falls back to auto-detection). This
module is the regression suite for every safety mechanism the script grew
because of that: identity refusal, the symlink guard, and -- found and fixed
in this same session, purely by running the script live -- the confirmation
diff correctly showing brand-new pages, not just modifications to existing
ones.

The script resolves its own repo from ${BASH_SOURCE[0]}/.. (same technique
as check-pii.sh), so a copy in a throwaway repo's scripts/ directory is
enough to test it in isolation. `WIKI_REPO_URL` is the script's own,
already-built-in seam for exactly this: point it at a local bare git repo
instead of github.com, and every clone/push in these tests stays entirely
on disk, never touching the network or the real wiki.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / 'scripts' / 'publish-wiki.sh'
CHECK_PII = REPO_ROOT / 'scripts' / 'check-pii.sh'


def _git(repo, *args, env=None):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, env=env)


def _isolated_env(tmp_path):
    """A subprocess environment with no ambient git identity to fall back
    on -- HOME points at an empty, freshly-created directory, so a test
    that unsets the repo's own local identity can't be rescued by whatever
    global .gitconfig happens to exist on the machine running the suite."""
    fake_home = tmp_path / 'fake-home'
    fake_home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env['HOME'] = str(fake_home)
    return env


@pytest.fixture
def main_repo(tmp_path):
    """A throwaway repo carrying copies of the two scripts under test
    (publish-wiki.sh calls check-pii.sh itself, so both are needed) and one
    committed wiki-drafts page."""
    repo = tmp_path / 'main'
    repo.mkdir()
    env = _isolated_env(tmp_path)
    _git(repo, 'init', '-q', '-b', 'main', env=env)
    _git(repo, 'config', 'user.email', 'author@example.com', env=env)
    _git(repo, 'config', 'user.name', 'Test Author', env=env)

    (repo / 'scripts').mkdir()
    shutil.copy(SCRIPT, repo / 'scripts' / 'publish-wiki.sh')
    shutil.copy(CHECK_PII, repo / 'scripts' / 'check-pii.sh')
    (repo / 'scripts' / 'publish-wiki.sh').chmod(0o755)
    (repo / 'scripts' / 'check-pii.sh').chmod(0o755)

    (repo / 'wiki-drafts').mkdir()
    (repo / 'wiki-drafts' / 'Home.md').write_text('# Home\n\nOriginal content.\n')

    _git(repo, 'add', '-A', env=env)
    _git(repo, 'commit', '-qm', 'baseline', env=env)
    return repo


def _seed_bare_wiki(tmp_path, name='wiki.git', files=None):
    """A bare git repo standing in for the real <repo>.wiki.git, seeded
    with an initial commit via a scratch working clone (mirroring how a
    real GitHub wiki always starts with at least one page)."""
    bare = tmp_path / name
    subprocess.run(['git', 'init', '--bare', '-q', '-b', 'master', str(bare)],
                    check=True, capture_output=True)

    seed = tmp_path / (name + '-seed')
    _git(tmp_path, 'clone', '-q', str(bare), str(seed))
    _git(seed, 'config', 'user.email', 'seed@example.com')
    _git(seed, 'config', 'user.name', 'Seed')
    for filename, content in (files or {'Home.md': 'Welcome to the wiki!\n'}).items():
        target = seed / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict) and content.get('symlink'):
            os.symlink(content['symlink'], target)
        else:
            target.write_text(content)
    _git(seed, 'add', '-A')
    _git(seed, 'commit', '-qm', 'seed')
    _git(seed, 'push', '-q', 'origin', 'master')
    return bare


def _run(repo, wiki_url, stdin_input, args=(), env=None):
    full_env = dict(env or os.environ)
    full_env['WIKI_REPO_URL'] = str(wiki_url)
    return subprocess.run(
        ['bash', 'scripts/publish-wiki.sh', *args],
        cwd=repo, input=stdin_input, capture_output=True, text=True, env=full_env,
    )


def _fetch_wiki_content(tmp_path, bare, filename, label):
    """Clones `bare` fresh (a new label each call, so paths never collide)
    and returns (content, author_line) as of its current HEAD."""
    clone = tmp_path / label
    _git(tmp_path, 'clone', '-q', str(bare), str(clone))
    content = (clone / filename).read_text() if (clone / filename).exists() else None
    author = subprocess.run(
        ['git', 'log', '-1', '--format=%an <%ae>'],
        cwd=clone, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return content, author


# ---------------------------- refusals ----------------------------

def test_refuses_when_wiki_drafts_has_uncommitted_changes(tmp_path, main_repo):
    bare = _seed_bare_wiki(tmp_path)
    (main_repo / 'wiki-drafts' / 'Home.md').write_text('# Home\n\nUncommitted edit.\n')

    result = _run(main_repo, bare, stdin_input='n\n')

    assert result.returncode == 1
    assert 'uncommitted changes' in result.stderr


def test_refuses_when_git_identity_is_unset(tmp_path, main_repo):
    bare = _seed_bare_wiki(tmp_path)
    env = _isolated_env(tmp_path)
    _git(main_repo, 'config', '--unset', 'user.name', env=env)
    _git(main_repo, 'config', '--unset', 'user.email', env=env)

    result = _run(main_repo, bare, stdin_input='n\n', env=env)

    assert result.returncode == 1
    assert "isn't set locally" in result.stderr


def test_refuses_when_cloned_wiki_contains_a_symlink(tmp_path, main_repo):
    bare = _seed_bare_wiki(tmp_path, files={
        'Home.md': 'Welcome!\n',
        'Sneaky.md': {'symlink': '/etc/passwd'},
    })

    result = _run(main_repo, bare, stdin_input='n\n')

    assert result.returncode == 1
    assert 'symlink' in result.stderr


def test_refuses_with_a_helpful_message_when_the_wiki_has_never_been_initialized(
        tmp_path, main_repo):
    """No page has ever been created for this wiki -- the equivalent of a
    real <repo>.wiki.git that GitHub hasn't provisioned yet."""
    never_initialized = tmp_path / 'does-not-exist.git'

    result = _run(main_repo, never_initialized, stdin_input='n\n')

    assert result.returncode == 1
    assert "hasn't been initialized yet" in result.stderr


# ---------------------------- the diff preview (regression) ----------------------------

def test_a_brand_new_page_appears_in_the_confirmation_diff(tmp_path, main_repo):
    """The exact bug found and fixed this session: a plain `git diff` shows
    nothing for an untracked (brand-new) file, so a page that doesn't exist
    in the wiki yet used to be silently absent from the preview a human
    reviews before confirming. Pin that it now shows up."""
    bare = _seed_bare_wiki(tmp_path)  # only has Home.md
    # Named FreshPage.md rather than a more obvious alternative -- a more
    # literal choice of filename here collides case-insensitively with a
    # real denylist term in check-pii.sh's --full scan (same category of
    # false-positive name collision documented in test_check_pii.py). Not
    # spelling out the collision itself, to avoid re-triggering it here.
    (main_repo / 'wiki-drafts' / 'FreshPage.md').write_text('# Fresh Page\n\nBrand new.\n')
    _git(main_repo, 'add', '-A', env=_isolated_env(tmp_path))
    _git(main_repo, 'commit', '-qm', 'add FreshPage.md', env=_isolated_env(tmp_path))

    result = _run(main_repo, bare, stdin_input='n\n')

    assert 'FreshPage.md' in result.stdout
    assert 'Brand new.' in result.stdout


# ---------------------------- confirm / decline ----------------------------

def test_declining_the_prompt_pushes_nothing(tmp_path, main_repo):
    bare = _seed_bare_wiki(tmp_path)

    result = _run(main_repo, bare, stdin_input='n\n')

    assert result.returncode == 1
    assert 'Aborted' in result.stdout
    content, _author = _fetch_wiki_content(tmp_path, bare, 'Home.md', 'after-decline')
    assert content == 'Welcome to the wiki!\n'  # unchanged


def test_confirming_publishes_with_the_configured_identity(tmp_path, main_repo):
    bare = _seed_bare_wiki(tmp_path)

    result = _run(main_repo, bare, stdin_input='y\n')

    assert result.returncode == 0
    assert '==> Done' in result.stdout
    content, author = _fetch_wiki_content(tmp_path, bare, 'Home.md', 'after-confirm')
    assert content == '# Home\n\nOriginal content.\n'
    # The whole point of this script: never an auto-detected name+hostname,
    # always exactly the identity configured in the main repo.
    assert author == 'Test Author <author@example.com>'


def test_nothing_to_publish_when_the_wiki_already_matches(tmp_path, main_repo):
    bare = _seed_bare_wiki(tmp_path, files={'Home.md': '# Home\n\nOriginal content.\n'})

    result = _run(main_repo, bare, stdin_input='n\n')

    assert result.returncode == 0
    assert 'Nothing to publish' in result.stdout


def test_only_named_pages_are_published(tmp_path, main_repo):
    (main_repo / 'wiki-drafts' / 'Second.md').write_text('# Second\n')
    _git(main_repo, 'add', '-A', env=_isolated_env(tmp_path))
    _git(main_repo, 'commit', '-qm', 'add Second.md', env=_isolated_env(tmp_path))
    bare = _seed_bare_wiki(tmp_path)

    result = _run(main_repo, bare, stdin_input='y\n', args=('Second.md',))

    assert result.returncode == 0
    content, _author = _fetch_wiki_content(tmp_path, bare, 'Second.md', 'named-page')
    assert content == '# Second\n'
    home_content, _ = _fetch_wiki_content(tmp_path, bare, 'Home.md', 'named-page-home')
    assert home_content == 'Welcome to the wiki!\n'  # untouched -- wasn't named


def test_a_typoed_page_name_is_skipped_not_created(tmp_path, main_repo):
    bare = _seed_bare_wiki(tmp_path)

    result = _run(main_repo, bare, stdin_input='n\n', args=('Nonexistent.md',))

    assert 'WARN' in result.stderr
    assert 'not in wiki-drafts' in result.stderr
