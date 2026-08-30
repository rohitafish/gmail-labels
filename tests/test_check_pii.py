"""Covers scripts/check-pii.sh -- the privacy guard behind the pre-push hook.

The script resolves its own repo from ${BASH_SOURCE[0]}/.., so copying it
into a throwaway git repo (the `repo` fixture) is enough to run it against
fixture commits instead of this project's real history. Every denylist used
below is synthetic and written into that throwaway repo -- never the real
.pii-denylist, which a session-scoped autouse fixture snapshots and confirms
untouched at the end of the run, so an accidental escape from tmp_path is
caught loudly rather than silently corrupting the real file.

What this module does NOT cover: whether the pre-push hook actually invokes
this script correctly (test_docs_and_guardrails.py checks the installed
hook hasn't drifted from the tracked template) or CI's --full invocation.

One property worth stating up front, since it shapes several tests below:
`git grep <tree>` scans a commit's full snapshot, not a diff against its
parent. Once offending content is committed, every later commit's tree
carries it forward too -- a --range covering only a later commit still sees
an earlier commit's still-present files. The `repo` fixture also gitignores
.pii-denylist, so a fixture denylist's own terms (written to that file by
_write_denylist) aren't swept into a commit by `git add -A` and don't make
the file match its own content.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'check-pii.sh'
REAL_DENYLIST = Path(__file__).resolve().parent.parent / '.pii-denylist'


@pytest.fixture(scope='session', autouse=True)
def _real_denylist_untouched():
    """Guards against a test escaping tmp_path and writing to the repo's own
    gitignored, real .pii-denylist. Absent is fine (a fresh clone has none);
    present-and-changed is a bug in a test, not a passing run."""
    before = hashlib.sha256(REAL_DENYLIST.read_bytes()).hexdigest() \
        if REAL_DENYLIST.exists() else None
    yield
    after = hashlib.sha256(REAL_DENYLIST.read_bytes()).hexdigest() \
        if REAL_DENYLIST.exists() else None
    assert before == after, 'a test modified the real .pii-denylist -- it escaped tmp_path'


def _git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo carrying a copy of the script under test."""
    _git(tmp_path, 'init', '-q', '-b', 'main')
    _git(tmp_path, 'config', 'user.email', 't@example.com')
    _git(tmp_path, 'config', 'user.name', 't')

    (tmp_path / 'scripts').mkdir()
    shutil.copy(SCRIPT, tmp_path / 'scripts' / 'check-pii.sh')
    # Ignore .pii-denylist so a fixture denylist's own terms (e.g.
    # 'brambleworth', written to that file by _write_denylist) aren't swept
    # into a commit by _commit_file's `git add -A` -- which would otherwise
    # make the file match its own content and fail every test that uses it.
    (tmp_path / '.gitignore').write_text('.pii-denylist\n')
    _git(tmp_path, 'add', '-A')
    _git(tmp_path, 'commit', '-qm', 'baseline')
    return tmp_path


def _write_denylist(repo, text):
    (repo / '.pii-denylist').write_text(text)


def _commit_file(repo, relpath, content):
    """Commits content at relpath (uncommitted, since .pii-denylist itself
    is meant to be gitignored and dev-machine-only, not tracked); returns
    the range covering just this commit."""
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', f'add {relpath}')
    return 'HEAD~1..HEAD'


def _run(repo, *args):
    return subprocess.run(
        ['bash', 'scripts/check-pii.sh', *args],
        cwd=repo, capture_output=True, text=True,
    )


# ---------------------------- denylist: bare substring ----------------------------

def test_bare_substring_matches_inside_a_longer_word(repo):
    _write_denylist(repo, 'brambleworth\n')
    rng = _commit_file(repo, 'note.py', '# client is called Brambleworth Holdings\n')

    result = _run(repo, '--range', rng)

    assert result.returncode == 1
    assert 'brambleworth' in result.stdout.lower()


def test_bare_substring_is_case_insensitive(repo):
    _write_denylist(repo, 'brambleworth\n')
    rng = _commit_file(repo, 'note.py', '# BRAMBLEWORTH appears here\n')

    result = _run(repo, '--range', rng)

    assert result.returncode == 1


def test_absence_of_the_denylist_term_is_clean(repo):
    _write_denylist(repo, 'brambleworth\n')
    rng = _commit_file(repo, 'note.py', '# nothing sensitive here\n')

    result = _run(repo, '--range', rng)

    assert result.returncode == 0


# ---------------------------- denylist: w: whole word ----------------------------

def test_w_prefix_matches_only_a_whole_word(repo):
    """The collision w: exists to solve: a name that's also an ordinary
    word must not fire on every unrelated word that merely contains it.
    'Wren' is both a common surname and an ordinary word (the bird), and
    sits inside the unrelated word 'wrench' -- exactly the collision shape
    this whole mechanism exists for."""
    _write_denylist(repo, 'w:wren\n')
    hit = _commit_file(repo, 'a.py', '# Wren called about the invoice\n')

    result = _run(repo, '--range', hit)
    assert result.returncode == 1


def test_w_prefix_does_not_match_inside_a_longer_word(repo):
    _write_denylist(repo, 'w:wren\n')
    rng = _commit_file(repo, 'a.py', '# tighten it with a wrench\n')

    result = _run(repo, '--range', rng)

    assert result.returncode == 0


def test_w_prefix_is_still_case_insensitive(repo):
    """w: is whole-word, but NOT case-sensitive -- that's W:'s job. A common
    lowercase word matching the denylist term still fires under w:, which is
    exactly why W: exists as an escape hatch for that specific collision."""
    _write_denylist(repo, 'w:wren\n')
    rng = _commit_file(repo, 'a.py', '# a wren landed on the fence\n')

    result = _run(repo, '--range', rng)

    assert result.returncode == 1


# ---------------------------- denylist: W: case-sensitive whole word ----------------------------

def test_capital_w_matches_the_capitalised_form(repo):
    _write_denylist(repo, 'W:Wren\n')
    rng = _commit_file(repo, 'a.py', '# Wren called about the invoice\n')

    result = _run(repo, '--range', rng)

    assert result.returncode == 1


def test_capital_w_does_not_match_the_lowercase_ordinary_word(repo):
    """The exact collision W: exists for: a real name that is also spelled
    like a common lowercase word. w: alone would fire on every 'wren' in
    ordinary prose; W: only fires on the capitalised, name-shaped form."""
    _write_denylist(repo, 'W:Wren\n')
    rng = _commit_file(repo, 'a.py', '# a wren landed on the fence\n')

    result = _run(repo, '--range', rng)

    assert result.returncode == 0


# ---------------------------- missing / empty denylist ----------------------------

def test_missing_denylist_warns_but_still_runs_structural_checks(repo):
    email = 'person' + '@' + 'realdomain.com'
    rng = _commit_file(repo, 'a.py', '# contact me at %s\n' % email)

    result = _run(repo, '--range', rng)

    assert result.returncode == 1
    assert '.pii-denylist not found' in result.stdout
    assert 'email' in result.stdout.lower()


def test_empty_denylist_warns_it_has_no_terms(repo):
    _write_denylist(repo, '# just a comment, no terms\n')
    rng = _commit_file(repo, 'a.py', 'nothing interesting\n')

    result = _run(repo, '--range', rng)

    assert result.returncode == 0
    assert 'no terms in it' in result.stdout


# ---------------------------- structural FAIL rules ----------------------------

# These build their trigger content by concatenation rather than as one
# contiguous literal, deliberately: check-pii.sh --full scans THIS repo's
# own history too (see CI and the pre-push hook), and this file's job is to
# put these exact shapes into a *fixture* commit inside a throwaway repo --
# not to also plant them, self-matchingly, in this file's own source text.
# The values still exist as ordinary strings by the time they're written to
# the fixture file below; only the static source of this file avoids
# spelling them out as one token.

def test_email_address_fails(repo):
    email = 'person' + '@' + 'realdomain.com'
    rng = _commit_file(repo, 'a.py', 'CONTACT = "%s"\n' % email)
    result = _run(repo, '--range', rng)
    assert result.returncode == 1


@pytest.mark.parametrize('domain', [
    'noreply@anthropic.com', 'someone@users.noreply.github.com',
    'x@anthropic.com', 'y@console.anthropic.com', 'test@example.com',
])
def test_allowlisted_email_domains_do_not_fail(repo, domain):
    rng = _commit_file(repo, 'a.py', 'CONTACT = "%s"\n' % domain)
    result = _run(repo, '--range', rng)
    assert result.returncode == 0


def test_gps_coordinate_pair_fails(repo):
    coords = '51.5074' + ', ' + '-0.1278'
    rng = _commit_file(repo, 'a.py', 'LOCATION = "%s"\n' % coords)
    result = _run(repo, '--range', rng)
    assert result.returncode == 1


def test_ssn_like_number_fails(repo):
    ssn = '123' + '-45-' + '6789'
    rng = _commit_file(repo, 'a.py', 'EXAMPLE = "%s"\n' % ssn)
    result = _run(repo, '--range', rng)
    assert result.returncode == 1


def test_uk_national_insurance_number_fails(repo):
    # D, F, I, Q, U, V are excluded from the first letter by the script's
    # own regex (see its comment) -- A and B are both valid first letters.
    nino = 'AB' + '123456' + 'C'
    rng = _commit_file(repo, 'a.py', 'NINO = "%s"\n' % nino)
    result = _run(repo, '--range', rng)
    assert result.returncode == 1


def test_uk_postcode_with_the_required_space_fails(repo):
    postcode = 'SW1A' + ' ' + '1AA'
    rng = _commit_file(repo, 'a.py', 'ADDRESS = "%s"\n' % postcode)
    result = _run(repo, '--range', rng)
    assert result.returncode == 1


def test_uk_postcode_shape_without_a_space_does_not_match_a_hex_colour(repo):
    """The mandatory space is deliberate: an optional space would also
    match hex colours and git-hash-derived filenames."""
    rng = _commit_file(repo, 'a.py', 'COLOR = "#1a2b3c"\n')
    result = _run(repo, '--range', rng)
    assert result.returncode == 0


# ---------------------------- structural WARN (non-blocking) rules ----------------------------

def test_uk_sort_code_warns_but_does_not_fail(repo):
    rng = _commit_file(repo, 'a.py', 'SORT_CODE = "12-34-56"\n')
    result = _run(repo, '--range', rng)
    assert result.returncode == 0
    assert 'sort code' in result.stdout.lower()


def test_uk_mobile_number_warns_but_does_not_fail(repo):
    rng = _commit_file(repo, 'a.py', 'MOBILE = "07123 456789"\n')
    result = _run(repo, '--range', rng)
    assert result.returncode == 0
    assert 'mobile' in result.stdout.lower()


def test_private_ipv4_addresses_are_excluded_entirely(repo):
    rng = _commit_file(repo, 'a.py', 'HOST = "192.168.1.50"\n')
    result = _run(repo, '--range', rng)
    assert result.returncode == 0
    assert 'IP' not in result.stdout


@pytest.mark.parametrize('ip', ['203.0.113.5', '8.8.8.8'])
def test_non_private_ipv4_addresses_warn_but_do_not_fail(repo, ip):
    rng = _commit_file(repo, 'a.py', 'HOST = "%s"\n' % ip)
    result = _run(repo, '--range', rng)
    assert result.returncode == 0
    assert 'non-private-looking IP' in result.stdout


def test_an_added_office_file_warns_but_does_not_fail(repo):
    target = repo / 'report.xlsx'
    target.write_bytes(b'PK\x03\x04fake-zip-bytes')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-qm', 'add report.xlsx')

    result = _run(repo, '--range', 'HEAD~1..HEAD')

    assert result.returncode == 0
    assert 'report.xlsx' in result.stdout
    assert "can't see inside this" in result.stdout


# ---------------------------- modes ----------------------------

def test_an_empty_range_is_clean(repo):
    _git(repo, 'branch', 'other', 'HEAD')
    result = _run(repo, '--range', 'HEAD..HEAD')
    assert result.returncode == 0
    assert 'nothing to check' in result.stdout


def test_full_mode_scans_every_commit_reachable_from_any_ref(repo):
    _write_denylist(repo, 'brambleworth\n')
    _commit_file(repo, 'old.py', '# Brambleworth was here from the start\n')
    _commit_file(repo, 'new.py', '# nothing sensitive\n')

    result = _run(repo, '--full')

    assert result.returncode == 1
    assert 'full history' in result.stdout


def test_a_commit_made_before_the_offending_content_existed_is_clean_on_its_own(repo):
    """`git grep <tree>` scans a commit's full snapshot, not a diff against
    its parent -- so once content is committed, every later commit's tree
    carries it forward too (a range covering only a *later* commit still
    sees an *earlier* commit's still-present files). The only commit with a
    genuinely clean tree is one made before the content ever existed --
    which --range accepts as a single revision, the same way `git rev-list`
    does."""
    baseline_sha = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    _write_denylist(repo, 'brambleworth\n')
    _commit_file(repo, 'old.py', '# Brambleworth was here\n')

    result = _run(repo, '--range', baseline_sha)

    assert result.returncode == 0
