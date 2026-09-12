"""Covers scripts/pii-denylist-sync.sh -- the routine that keeps
.pii-denylist in step with the live label tree.

Every case here drives the script through --from-csv, its offline input
path, so nothing touches the real mailbox: the filtering and block-rewriting
below the fetch is the same code either way, and it is where all the
behaviour worth pinning lives.

The invariants:

  * hand-written lines above the marker survive untouched;
  * the auto block is replaced whole, so a label you have since deleted
    drops out of it;
  * a candidate that would immediately break the repo -- an ordinary
    dictionary word, or a word already present in tracked content -- is
    reported, never added (denylisting one of those makes the very next
    check-pii.sh run FAIL on content that is already public);
  * an empty result never empties the file;
  * no value is printed unless --show-skipped explicitly asks for one.

The dictionary is pointed at a fixture word list via PII_SYNC_DICT rather
than the host's /usr/share/dict/words, so these tests mean the same thing on
a machine whose system dictionary is absent (most Linux installs) or
different.
"""

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / 'scripts' / 'pii-denylist-sync.sh'
REAL_DENYLIST = REPO_ROOT / '.pii-denylist'
BEGIN = '# >>> auto-synced from the live label tree by scripts/pii-denylist-sync.sh'

# Deliberately invented names that are not English words, so the dictionary
# rule can't reject them and the tests stay about the logic under test.
LABELS = """LABEL,LABEL_ID
Dosh/Zzyzxbank/Statements,L1
Dosh/Travel,L2
Journeys/Quibblesworth Client Ltd,L3
Politics/Old/PFI,L4
"""

HAND = '# my own lines\nSome Real Name\n'

# Enough of a word list to exercise the rule; "travel" and "statements"
# (via its "statement" stem) are the ordinary words in LABELS above.
WORDS = 'travel\nstatement\npolitics\njourney\ndosh\nold\n'


@pytest.fixture(scope='module', autouse=True)
def _real_denylist_untouched():
    """This script's whole job is writing .pii-denylist, so a test that
    escaped tmp_path would overwrite the real one. Same guard as
    test_check_pii.py's."""
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
    """A throwaway repo carrying a copy of the script at its real relative
    path (it resolves its own repo root from ${BASH_SOURCE[0]}/..), one
    committed file, and a fixture word list."""
    _git(tmp_path, 'init', '-q', '-b', 'main')
    _git(tmp_path, 'config', 'user.email', 't@example.com')
    _git(tmp_path, 'config', 'user.name', 't')

    (tmp_path / 'scripts').mkdir()
    script = tmp_path / 'scripts' / 'pii-denylist-sync.sh'
    script.write_text(SCRIPT.read_text())
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    (tmp_path / '.gitignore').write_text('.pii-denylist\n.pii-denylist-skip\n')
    (tmp_path / 'README.md').write_text('nothing personal here\n')
    _git(tmp_path, 'add', '-A')
    _git(tmp_path, 'commit', '-qm', 'baseline')

    (tmp_path / 'words.txt').write_text(WORDS)
    return tmp_path


def _run(repo, *args, csv=LABELS, denylist=HAND):
    if csv is not None:
        (repo / 'labels.csv').write_text(csv)
    if denylist is not None:
        (repo / '.pii-denylist').write_text(denylist)
    env = {**os.environ, 'PII_SYNC_DICT': str(repo / 'words.txt')}
    return subprocess.run(
        ['bash', 'scripts/pii-denylist-sync.sh', '--from-csv', 'labels.csv', *args],
        cwd=repo, capture_output=True, text=True, env=env, timeout=120,
    )


def _block(repo):
    text = (repo / '.pii-denylist').read_text()
    assert BEGIN in text
    return text.split(BEGIN, 1)[1]


def test_distinctive_segments_are_added_as_whole_word_terms(repo):
    result = _run(repo)

    assert result.returncode == 0, result.stderr
    block = _block(repo)
    assert 'w:Zzyzxbank' in block
    assert 'w:Quibblesworth Client Ltd' in block


def test_hand_written_lines_above_the_marker_are_untouched(repo):
    _run(repo)

    assert (repo / '.pii-denylist').read_text().startswith(HAND)


def test_ordinary_dictionary_words_are_skipped(repo):
    """'Travel' and 'Statements' are real segments of the fixture tree and
    also ordinary English -- denylisting either would block every later
    commit that used the word in prose."""
    result = _run(repo)

    block = _block(repo)
    assert 'Travel' not in block
    assert 'Statements' not in block   # matched via its 'statement' stem
    assert 'ordinary words' in result.stdout


def test_a_segment_already_in_tracked_content_is_reported_not_added(repo):
    """Adding a term that is already in a tracked file would make the very
    next check-pii.sh run FAIL against content that has been public since
    the first push."""
    (repo / 'README.md').write_text('we talk about Zzyzxbank in the docs\n')
    _git(repo, 'commit', '-qam', 'mention it')

    result = _run(repo)

    assert 'Zzyzxbank' not in _block(repo)
    assert '1 already in tracked content' in result.stdout


def test_the_skip_file_excludes_a_candidate_permanently(repo):
    """The auto block is rewritten whole every run, so deleting a line from
    it is not durable -- .pii-denylist-skip is."""
    (repo / '.pii-denylist-skip').write_text('zzyzxbank\n')

    result = _run(repo)

    assert 'Zzyzxbank' not in _block(repo)
    assert '1 in .pii-denylist-skip' in result.stdout


def test_a_term_already_hand_listed_is_not_duplicated_into_the_block(repo):
    result = _run(repo, denylist=HAND + 'w:Zzyzxbank\n')

    assert 'Zzyzxbank' not in _block(repo)
    assert '1 already hand-listed' in result.stdout


def test_the_block_is_replaced_whole_so_a_deleted_label_drops_out(repo):
    first = _run(repo)
    assert first.returncode == 0, first.stderr
    assert 'Zzyzxbank' in _block(repo)

    # The bank label is gone from the mailbox; the client label remains.
    second = _run(
        repo,
        csv='LABEL,LABEL_ID\nJourneys/Quibblesworth Client Ltd,L3\n',
        denylist=None,  # keep what the first run wrote
    )

    assert second.returncode == 0, second.stderr
    text = (repo / '.pii-denylist').read_text()
    assert 'Zzyzxbank' not in text
    assert 'w:Quibblesworth Client Ltd' in text
    assert 'Some Real Name' in text  # hand-written survives both runs


def test_a_label_containing_a_comma_is_not_truncated(repo):
    """The CSV is read as CSV, not split on commas -- a quoted label with a
    comma in it would otherwise be silently shortened to its first half,
    and a shortened term is a term that doesn't match the real value."""
    result = _run(repo, csv='LABEL,LABEL_ID\n"Friends/Blathersby, Wexforth and Co",L9\n')

    assert result.returncode == 0, result.stderr
    assert 'w:Blathersby, Wexforth and Co' in _block(repo)


def test_no_value_is_printed_unless_asked(repo):
    result = _run(repo)

    for value in ('Zzyzxbank', 'Quibblesworth', 'Travel', 'Some Real Name'):
        assert value not in result.stdout + result.stderr


def test_show_skipped_prints_the_rejected_candidates(repo):
    """The counts alone can't tell you whether the filters threw away
    something that mattered; this is the deliberate, opt-in way to look."""
    result = _run(repo, '--show-skipped')

    assert 'Travel' in result.stdout
    assert 'dictionary word' in result.stdout


def test_dry_run_writes_nothing(repo):
    result = _run(repo, '--dry-run')

    assert result.returncode == 0
    assert 'dry run' in result.stdout
    assert (repo / '.pii-denylist').read_text() == HAND


def test_an_empty_label_tree_refuses_to_touch_the_file(repo):
    result = _run(repo, csv='LABEL,LABEL_ID\n')

    assert result.returncode == 1
    assert (repo / '.pii-denylist').read_text() == HAND


def test_a_tree_where_everything_is_filtered_out_refuses_too(repo):
    """Distinct from the empty case above: labels came back, but none of
    them survived the filters. Writing the empty block that would produce
    silently disarms the denylist."""
    result = _run(repo, csv='LABEL,LABEL_ID\nDosh/Travel,L2\nPolitics/Old,L4\n')

    assert result.returncode == 1
    assert (repo / '.pii-denylist').read_text() == HAND


def test_the_written_file_is_owner_only(repo):
    _run(repo)

    mode = stat.S_IMODE((repo / '.pii-denylist').stat().st_mode)
    assert mode == 0o600


def test_a_missing_csv_is_an_error_that_changes_nothing(repo):
    result = _run(repo, csv=None)

    assert result.returncode == 3
    assert (repo / '.pii-denylist').read_text() == HAND


def test_unknown_flag_is_a_usage_error(repo):
    assert _run(repo, '--bogus').returncode == 2


def test_a_segment_only_present_in_an_old_commit_is_still_skipped(repo):
    """The tracked-content rule looks at the whole history, not just HEAD.
    A term edited out of the tip but still sitting in an old commit would
    otherwise be added -- and then make every `check-pii.sh --full` run FAIL
    for good, against content that has been public since it was pushed."""
    (repo / 'README.md').write_text('we used to talk about Zzyzxbank here\n')
    _git(repo, 'commit', '-qam', 'mention it')
    (repo / 'README.md').write_text('not any more\n')
    _git(repo, 'commit', '-qam', 'take it out again')

    result = _run(repo)

    assert 'Zzyzxbank' not in _block(repo)
    assert '1 already in tracked content' in result.stdout
