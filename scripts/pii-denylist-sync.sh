#!/usr/bin/env bash
# Keep .pii-denylist in step with the live label tree, so scripts/check-pii.sh
# can actually do its job. Run from the dev machine (the denylist is
# gitignored and dev-machine-only).
#
#   scripts/pii-denylist-sync.sh                    # sync from the live mailbox
#   scripts/pii-denylist-sync.sh --dry-run          # report what would change, write nothing
#   scripts/pii-denylist-sync.sh --from-csv F.csv   # offline, from an audit CSV's LABEL column
#   scripts/pii-denylist-sync.sh --show-skipped     # print the rejected candidates (values!)
#
# Why: the denylist is hand-maintained, and a check that knows a fraction of
# the real values catches a fraction of the leaks. This mailbox's label tree
# already IS the authoritative list of the bank, client and family names that
# must never reach GitHub -- every one of them is a segment of some label
# path. This reads them instead of waiting for someone to remember one.
#
# The sibling project's equivalent (assetmgt's scripts/pii-denylist-sync.sh)
# pulls MACs and serials out of a device inventory, where every pulled value
# is unambiguously an identifier. Label segments are not: "Travel" and
# "Receipts" are real segments here and also ordinary English that belongs in
# ordinary prose. Adding one of those to the denylist doesn't just add noise,
# it blocks every future commit that uses the word. So a candidate is kept
# only if it survives all four filters:
#
#   1. at least 4 characters (shorter is too collision-prone to be useful);
#   2. not an ordinary dictionary word -- checked against /usr/share/dict/words,
#      with a trailing s/es stripped first, so "Journeys" -> "journey" is
#      rejected while an invented or proper name survives. Single-word
#      candidates only: a multi-word segment is distinctive by construction;
#   3. not already in the repo's own tracked content -- if the word is
#      already legitimately in a tracked file, denylisting it would make the
#      very next check-pii.sh run FAIL on content that has been public since
#      the first push. Those are reported as a count to review, never added;
#   4. not listed in .pii-denylist-skip (gitignored, one term per line) --
#      the escape hatch for a candidate that gets past 1-3 and still
#      shouldn't be a denylist term. The auto block is rewritten whole on
#      every run, so this file is the only durable way to say "not that one".
#
# Kept entries are written with the `w:` (whole-word) prefix that
# check-pii.sh understands, not as bare substrings: a surname that is also a
# word-fragment would otherwise match inside unrelated words.
#
# The pulled values go in a marked block at the END of .pii-denylist;
# everything above the marker is yours and is never touched. The block is
# rewritten whole each run, so a label you have since deleted drops out of it
# (keep it by moving it above the marker).
#
# Never prints a value unless you ask with --show-skipped. Same invariant as
# the sibling project's copy.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DENYLIST="$REPO_DIR/.pii-denylist"
SKIPFILE="$REPO_DIR/.pii-denylist-skip"
DICT="${PII_SYNC_DICT:-/usr/share/dict/words}"
DRY_RUN=0
SHOW_SKIPPED=0
FROM_CSV=""
BEGIN_MARK="# >>> auto-synced from the live label tree by scripts/pii-denylist-sync.sh -- edit ABOVE this line, not below"
END_MARK="# <<< end auto-synced"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --show-skipped) SHOW_SKIPPED=1 ;;
    --from-csv) FROM_CSV="${2:?--from-csv needs a path}"; shift ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

PYTHON="$REPO_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

# The fetch snippets are single-quoted shell strings passed to `python -c`,
# not heredocs inside $( ): macOS ships bash 3.2, whose parser mishandles a
# here-document inside a command substitution (it fails to find the closing
# paren and reports "unexpected EOF" -- confirmed while writing this). The
# Python below therefore uses double quotes only, so it can live inside a
# single-quoted shell string untouched.
FETCH_CSV='
# csv, not cut -d, -- a label containing a comma is quoted in the file and
# would otherwise be truncated at the comma, silently shortening the term.
import csv, sys
with open(sys.argv[1], newline="", encoding="utf-8-sig") as fh:
    for row in csv.DictReader(fh):
        name = (row.get("LABEL") or "").strip()
        if name:
            print(name)
'

FETCH_LIVE='
# The live tree, over the read-only token the audit already uses.
# Deliberately pre-flighted: gmail_common.service() falls back to an
# interactive browser flow when the stored token cannot be refreshed, which
# is right for `./run audit` and wrong for a script that might run
# unattended. If the token is missing or dead, say so and exit rather than
# popping a browser open.
import os, sys
repo = sys.argv[1]
sys.path.insert(0, repo)
token = os.path.join(repo, "token_readonly.json")
if not os.path.exists(token):
    sys.exit("no token_readonly.json -- run `./run audit` once to authorise, "
             "or pass --from-csv with an existing label_audit.csv")
import gmail_common as gc
from google.oauth2.credentials import Credentials
try:
    creds = Credentials.from_authorized_user_file(token, [gc.SCOPE_READONLY])
except ValueError:
    sys.exit("token_readonly.json is unreadable -- re-authorise with `./run audit`")
if not creds.valid and not (creds.expired and creds.refresh_token):
    sys.exit("token_readonly.json cannot be refreshed without a browser -- "
             "re-authorise with `./run audit`, or pass --from-csv")
for label in gc.list_user_labels(gc.service(gc.SCOPE_READONLY, "token_readonly.json")):
    print(label["name"])
'

# One fetch. Output: one label NAME per line, nothing else -- all the
# filtering below works on that, so the offline --from-csv path and the live
# path are the same code from here down.
if [ -n "$FROM_CSV" ]; then
  [ -f "$FROM_CSV" ] || { echo "!!! no such file: $FROM_CSV" >&2; exit 3; }
  RAW="$("$PYTHON" -c "$FETCH_CSV" "$FROM_CSV")" \
    || { echo "!!! could not read $FROM_CSV -- nothing changed." >&2; exit 3; }
else
  RAW="$("$PYTHON" -c "$FETCH_LIVE" "$REPO_DIR")" \
    || { echo "!!! could not read the live label tree -- nothing changed." >&2; exit 3; }
fi

# Label path -> candidate segments. Emoji and other non-ASCII decoration are
# stripped ("Politics <emoji>" is the segment, the pictogram isn't part of the
# name anyone would leak), then whitespace is collapsed and trimmed.
CANDIDATES="$(printf '%s\n' "$RAW" \
  | tr '/' '\n' \
  | LC_ALL=C sed -e 's/[^ -~]//g' -e 's/[[:space:]][[:space:]]*/ /g' -e 's/^ *//' -e 's/ *$//' \
  | grep -v '^$' | sort -u || true)"

[ -n "$CANDIDATES" ] || { echo "!!! no label segments returned -- refusing to write an empty block." >&2; exit 1; }

# Split the existing file into the hand-written part and the old block, so
# the hand-written terms can also be skipped as already-covered.
if [ -f "$DENYLIST" ]; then
  HAND="$(awk -v b="$BEGIN_MARK" '$0 == b {exit} {print}' "$DENYLIST")"
  OLD_BLOCK="$(awk -v b="$BEGIN_MARK" -v e="$END_MARK" '$0 == e {f=0} f {print} $0 == b {f=1}' "$DENYLIST")"
else
  HAND="# Literal strings that must NEVER appear in a commit to this repo.
# One per line. Case-insensitive substring match by default; prefix \`w:\`
# for whole-word (still case-insensitive), or \`W:\` for whole-word AND
# case-sensitive (see scripts/check-pii.sh)."
  OLD_BLOCK=""
fi
HAND_TERMS="$(printf '%s\n' "$HAND" | grep -vE '^\s*(#|$)' | sed -e 's/^[wW]://' | tr 'A-Z' 'a-z' || true)"
SKIP_TERMS=""
[ -f "$SKIPFILE" ] && SKIP_TERMS="$(grep -vE '^\s*(#|$)' "$SKIPFILE" | tr 'A-Z' 'a-z' || true)"

# Every commit, not just HEAD: check-pii.sh --full scans the whole history,
# so a term that has been edited out of the tip but still sits in an old
# commit would make every later --full run FAIL for good. 3.2-safe array
# build (no mapfile), and the list is computed once rather than per term.
REV_ARR=()
while IFS= read -r rev; do
  [ -n "$rev" ] && REV_ARR+=("$rev")
done <<< "$(git -C "$REPO_DIR" rev-list --all 2>/dev/null)"
HAVE_HISTORY=0
[ "${#REV_ARR[@]}" -gt 0 ] && HAVE_HISTORY=1
HAVE_DICT=0
[ -r "$DICT" ] && HAVE_DICT=1
[ "$HAVE_DICT" -eq 1 ] || echo "  (no dictionary at $DICT -- ordinary words can only be filtered by the tracked-content rule below)"

# Pass 1: the cheap, local filters -- length, the skip file, terms already
# hand-listed, and the dictionary.
#
# One awk pass over the candidate list, with the three lists it checks
# against loaded into awk's own hash tables first, rather than a shell loop
# doing per-candidate `grep`/`tr`/pattern matching. Everything in here is
# batched for the same reason: the first working version of this routine
# asked its questions one candidate at a time and took ~85s against a real
# label tree (~1000 segments); this one takes ~3s end to end. A routine
# slow enough to avoid running is a routine that stops being run.
# FILENAME comparison (not gawk's ARGIND) is what makes the multi-file load
# portable to macOS's awk.
#
# The dictionary forms are the lowercased term, minus a trailing "s", and
# minus a trailing "es", so "Journeys" is rejected via "journey" and
# "Statements" via "statement". Single-word candidates only -- a multi-word
# segment is distinctive by construction, and no word list holds it anyway.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

printf '%s\n' "$CANDIDATES" > "$WORK/cand"
printf '%s\n' "$SKIP_TERMS" > "$WORK/skip"
printf '%s\n' "$HAND_TERMS" > "$WORK/hand"
: > "$WORK/dict"
if [ "$HAVE_DICT" -eq 1 ]; then
  awk '{ if (index($0, " ") || $0 == "") next
         l = tolower($0); print l
         s = l; sub(/s$/, "", s); print s
         if (l ~ /es$/) { e = l; sub(/es$/, "", e); print e } }' "$WORK/cand" \
    | sort -u > "$WORK/forms"
  # awk, not `grep -Fxi -f`: BSD grep has no multi-pattern automaton, so a
  # thousand patterns against a 235k-line word list is effectively a
  # thousand passes over it -- that one line was 34 of this routine's last
  # 37 seconds. One hashed pass is ~0.2s.
  awk 'NR == FNR { want[$0] = 1; next } { l = tolower($0); if (l in want) print l }' \
    "$WORK/forms" "$DICT" 2>/dev/null | sort -u > "$WORK/dict" || true
fi

awk -v skipf="$WORK/skip" -v handf="$WORK/hand" -v dictf="$WORK/dict" '
  FILENAME == skipf { if (length($0)) skip[tolower($0)] = 1; next }
  FILENAME == handf { if (length($0)) hand[tolower($0)] = 1; next }
  FILENAME == dictf { if (length($0)) dict[tolower($0)] = 1; next }
  {
    term = $0
    if (term == "") next
    lower = tolower(term)
    if (length(term) < 4)  { print "short\t" term; next }
    if (lower in skip)     { print "listed\t" term; next }
    if (lower in hand)     { print "hand\t" term; next }
    if (index(term, " ") == 0) {
      stem = lower; sub(/s$/, "", stem)
      sing = lower; sub(/es$/, "", sing)
      if ((lower in dict) || (stem in dict) || (sing in dict)) { print "dict\t" term; next }
    }
    print "keep\t" term
  }' "$WORK/skip" "$WORK/hand" "$WORK/dict" "$WORK/cand" > "$WORK/classified"

SKIP_SHORT="$(grep -c '^short	' "$WORK/classified" || true)"
SKIP_LISTED="$(grep -c '^listed	' "$WORK/classified" || true)"
SKIP_HAND="$(grep -c '^hand	' "$WORK/classified" || true)"
SKIP_DICT="$(grep -c '^dict	' "$WORK/classified" || true)"
SKIP_TRACKED=0
SURVIVORS="$(sed -n 's/^keep	//p' "$WORK/classified")"
SKIPPED_VALUES="$(sed -n 's/^dict	/  dictionary word: /p' "$WORK/classified")"

# Pass 2: is the term already in this repo's own tracked content? If it is,
# denylisting it would fail the next check-pii.sh run against content that
# is already on GitHub -- so it's reported, never auto-added. (If one of
# these really is a leak it needs a history rewrite, not a denylist entry.)
#
# Every commit, not just HEAD: check-pii.sh --full scans the whole history,
# so a term edited out of the tip but still sitting in an old commit would
# make every later --full run FAIL for good.
#
# One `git grep` for every surviving term at once, with -o so the output is
# the matched TEXT -- which is what makes a single invocation enough to say
# *which* terms hit. Asking that question per term instead costs a git grep
# each (~700 of them, ~50s against a real label tree); this is one.
printf '%s\n' "$SURVIVORS" > "$WORK/survivors"
: > "$WORK/tracked"
if [ "$HAVE_HISTORY" -eq 1 ] && [ -n "$SURVIVORS" ]; then
  git -C "$REPO_DIR" --no-pager grep --no-color -h -iwF -o -f "$WORK/survivors" "${REV_ARR[@]}" -- 2>/dev/null \
    | tr 'A-Z' 'a-z' | sort -u > "$WORK/tracked" || true
fi

awk -v trackedf="$WORK/tracked" '
  FILENAME == trackedf { if (length($0)) tracked[tolower($0)] = 1; next }
  {
    if ($0 == "") next
    if (tolower($0) in tracked) { print "tracked\t" $0 } else { print "keep\t" $0 }
  }' "$WORK/tracked" "$WORK/survivors" > "$WORK/final"

SKIP_TRACKED="$(grep -c '^tracked	' "$WORK/final" || true)"
KEPT="$(sed -n 's/^keep	/w:/p' "$WORK/final")"
TRACKED_VALUES="$(sed -n 's/^tracked	/  already in tracked content: /p' "$WORK/final")"
if [ -n "$TRACKED_VALUES" ]; then
  SKIPPED_VALUES="${SKIPPED_VALUES}${SKIPPED_VALUES:+$'\n'}${TRACKED_VALUES}"
fi

NEW_BLOCK="$(printf '%s\n' "$KEPT" | grep . | sort -u || true)"
NEW_COUNT="$(printf '%s\n' "$NEW_BLOCK" | grep -c . || true)"
[ "$NEW_COUNT" -gt 0 ] || { echo "!!! every candidate was filtered out -- refusing to write an empty block." >&2; exit 1; }

OLD_SORTED="$(printf '%s\n' "$OLD_BLOCK" | grep . | sort -u || true)"
OLD_COUNT="$(printf '%s\n' "$OLD_SORTED" | grep -c . || true)"
ADDED="$(comm -13 <(printf '%s\n' "$OLD_SORTED") <(printf '%s\n' "$NEW_BLOCK") | grep -c . || true)"
REMOVED="$(comm -23 <(printf '%s\n' "$OLD_SORTED") <(printf '%s\n' "$NEW_BLOCK") | grep -c . || true)"
HAND_COUNT="$(printf '%s\n' "$HAND_TERMS" | grep -c . || true)"
CAND_COUNT="$(printf '%s\n' "$CANDIDATES" | grep -c . || true)"

echo "== .pii-denylist sync from ${FROM_CSV:-the live label tree} =="
echo "  label segments seen  : $CAND_COUNT"
echo "  hand-written entries : $HAND_COUNT (untouched)"
echo "  skipped              : $SKIP_SHORT too short, $SKIP_DICT ordinary words, $SKIP_TRACKED already in tracked content, $SKIP_LISTED in .pii-denylist-skip, $SKIP_HAND already hand-listed"
echo "  auto block           : $OLD_COUNT -> $NEW_COUNT entries (+$ADDED / -$REMOVED)"
if [ "$SHOW_SKIPPED" -eq 1 ] && [ -n "$SKIPPED_VALUES" ]; then
  echo "  -- skipped candidates (values shown on request) --"
  printf '%s\n' "$SKIPPED_VALUES"
fi
if [ "$DRY_RUN" -eq 1 ]; then echo "  (dry run -- nothing written)"; exit 0; fi

TMP="$(mktemp "$REPO_DIR/.pii-denylist.XXXXXX")"
{
  printf '%s\n' "$HAND" | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}'
  printf '\n%s\n' "$BEGIN_MARK"
  printf '%s\n' "$NEW_BLOCK"
  printf '%s\n' "$END_MARK"
} > "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$DENYLIST"
echo "  written: $DENYLIST ($(grep -vcE '^\s*(#|$)' "$DENYLIST") entries in total)"
