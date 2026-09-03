#!/usr/bin/env bash
# Scans commits for PII before they reach GitHub: known real values from
# .pii-denylist, plus generic structural patterns (emails, GPS coordinates,
# non-private IPs, SSN-like numbers, UK National Insurance numbers, UK
# postcodes) as defense in depth. This repo used to rely on a machine-wide
# version of this same check (~/.pii-guardrail/, wired in via git's global
# core.hooksPath); that was decommissioned in favor of each project running
# its own local, hand-tuned copy instead -- one shared cross-project
# denylist means a term that's safe in one project's context can collide
# with ordinary language in another's (that's exactly what happened: one
# global entry, a proper name, matched the common-word lowercase form of
# that same string in an unrelated project's prose). This script and its
# denylist are ported from that global version, adapted to live inside
# this repo.
#
# Deliberately no `set -e`: report every problem in one pass, don't stop at
# the first. Exits 1 on any FAIL.
#
# Usage:
#   scripts/check-pii.sh                  # commits about to be pushed
#                                          # (upstream..HEAD, or origin/main..HEAD)
#   scripts/check-pii.sh --range A..B     # a specific range (the pre-push
#                                          # hook passes what git gives it)
#   scripts/check-pii.sh --full           # every commit reachable from any
#                                          # ref -- the whole history, not
#                                          # just what's about to move
#
# Denylist entries (.pii-denylist, sibling to this script's parent dir)
# match as a case-insensitive literal SUBSTRING by default -- fine for
# distinctive values (a surname, a bank name) but unusable for a term
# that's also an ordinary English word: some proper names are also common
# words or word-fragments, and will match inside a completely unrelated
# word that happens to contain that same letter sequence. Prefix a line
# with `w:` to match as a whole word instead (git grep -w) -- but `w:` is
# still case-insensitive, so it doesn't help when the term collides with a
# *standalone* common word too, not just a substring of one (a name and
# the ordinary lowercase word it's also spelled like both hit as whole
# words under `w:`). Prefix with `W:` (capital) instead for a
# case-SENSITIVE whole-word match: it only fires on the exact-case form,
# so a real name still gets caught (including at the start of a sentence,
# where it's capitalized anyway) while the lowercase common word doesn't.
# Reach for `W:` only when `w:` itself produces a false positive on real
# usage -- it's strictly narrower, so it also stops catching a
# deliberately-miscapitalized real occurrence (rare, but the tradeoff).
# Use plain substring for distinctive values, `w:` for ones that collide
# with real words, `W:` only for ones that have actually collided even
# under `w:`.
#
# Denylist matching is batched: ALL terms in a tier are checked in a
# single `git grep -f <patternfile>` pass across every commit at once
# (rather than one invocation per term per commit) -- with N terms and M
# commits that's the difference between ~3 invocations and N*M. Once a
# batched pass finds a hit, the *specific* matched commits (usually a
# small set, ideally zero) are re-checked term-by-term to produce the
# precise "denylist term 'X' found in <commit>:<file>" message -- the
# per-term loop only ever runs over already-known hits, not the whole
# history.
#
# What this can't catch: a *new* real name used for the first time as an
# example. It isn't in the denylist yet (nothing is, until someone notices
# and adds it), and a name is syntactically indistinguishable from any
# other word, so no pattern can flag it. The denylist and the patterns
# below are a backstop for *known* values and *structural* PII, not a
# substitute for never inventing illustrative examples from real personal
# details in the first place. It also can't see inside a binary/office
# file's content -- see the WARN below, which flags that a human needs to
# check one by hand.
#
# Every `git grep` below passes --no-color explicitly, regardless of the
# caller's gitconfig -- `color.ui=always` (as opposed to the default
# `auto`) makes git emit real ANSI escape codes even when output is
# piped/captured into a variable, not just on a terminal. Without
# --no-color those escape bytes land inside the captured string and
# silently break exact-match logic further down.
#
# No associative arrays or `mapfile` -- macOS's own /bin/bash is the
# ancient 3.2. Indexed arrays built with a `while read` loop are the
# 3.2-safe way to turn command output into an array.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

FAILS=0

ok()   { printf '  ok    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }

MODE="range"
RANGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --full) MODE="full"; shift ;;
    --range) RANGE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$MODE" = "range" ] && [ -z "$RANGE" ]; then
  UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  if [ -n "$UPSTREAM" ]; then
    RANGE="$UPSTREAM..HEAD"
  elif git rev-parse --verify origin/main >/dev/null 2>&1; then
    RANGE="origin/main..HEAD"
  else
    warn "no upstream branch and no origin/main found -- checking full history instead"
    MODE="full"
  fi
fi

# .pii-denylist is gitignored and dev-machine-only -- the one sanctioned
# place these real values live in plaintext, same logic as credentials.json
# for secrets. Its absence is a WARN, not a FAIL: a fresh clone hasn't
# populated it yet, and the generic pattern checks below still run.
DENYLIST_FILE="$REPO_DIR/.pii-denylist"
DENY_SUBSTR=()
DENY_WORD=()
DENY_WORD_CS=()

if [ -f "$DENYLIST_FILE" ]; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    case "$line" in
      W:*) DENY_WORD_CS+=("${line#W:}") ;;
      w:*) DENY_WORD+=("${line#w:}") ;;
      *)   DENY_SUBSTR+=("$line") ;;
    esac
  done < "$DENYLIST_FILE"
  if [ "${#DENY_SUBSTR[@]}" -eq 0 ] && [ "${#DENY_WORD[@]}" -eq 0 ] && [ "${#DENY_WORD_CS[@]}" -eq 0 ]; then
    warn ".pii-denylist exists but has no terms in it"
  fi
else
  warn ".pii-denylist not found -- skipping known-value checks; generic pattern checks still run."
fi

if [ "$MODE" = "full" ]; then
  COMMITS="$(git rev-list --all 2>/dev/null)"
  LABEL="full history"
else
  COMMITS="$(git rev-list "$RANGE" 2>/dev/null)"
  LABEL="range $RANGE"
fi

echo "== PII check: $LABEL =="

if [ -z "$COMMITS" ]; then
  ok "$LABEL: nothing to check (no commits in range)"
  echo
  echo "== Summary =="
  echo "  $FAILS FAIL(s)"
  exit 0
fi

# 3.2-safe: build an array of commit shas from $COMMITS without mapfile, so
# they can be passed to `git grep` as separate <tree> arguments in one call
# rather than one invocation per commit via xargs.
COMMIT_ARR=()
while IFS= read -r c; do
  [ -n "$c" ] && COMMIT_ARR+=("$c")
done <<< "$COMMITS"

HIT=0

SUBSTR_PATFILE=""
WORD_PATFILE=""
WORD_CS_PATFILE=""
cleanup() {
  [ -n "$SUBSTR_PATFILE" ] && rm -f "$SUBSTR_PATFILE"
  [ -n "$WORD_PATFILE" ] && rm -f "$WORD_PATFILE"
  [ -n "$WORD_CS_PATFILE" ] && rm -f "$WORD_CS_PATFILE"
}
trap cleanup EXIT

if [ "${#DENY_SUBSTR[@]}" -gt 0 ]; then
  SUBSTR_PATFILE="$(mktemp)"
  printf '%s\n' "${DENY_SUBSTR[@]}" > "$SUBSTR_PATFILE"

  MATCHES="$(git --no-pager grep --no-color -i -F -l -f "$SUBSTR_PATFILE" "${COMMIT_ARR[@]}" -- 2>/dev/null)"
  if [ -n "$MATCHES" ]; then
    HIT=1
    HIT_COMMITS="$(echo "$MATCHES" | cut -d: -f1 | sort -u)"
    for term in "${DENY_SUBSTR[@]}"; do
      term_matches="$(echo "$HIT_COMMITS" | xargs -I{} git --no-pager grep --no-color -i -F -l -e "$term" {} -- 2>/dev/null)"
      if [ -n "$term_matches" ]; then
        while IFS= read -r m; do
          fail "denylist term '$term' found in $m"
        done <<< "$term_matches"
      fi
    done
  fi
fi

if [ "${#DENY_WORD[@]}" -gt 0 ]; then
  WORD_PATFILE="$(mktemp)"
  printf '%s\n' "${DENY_WORD[@]}" > "$WORD_PATFILE"

  MATCHES="$(git --no-pager grep --no-color -i -w -F -l -f "$WORD_PATFILE" "${COMMIT_ARR[@]}" -- 2>/dev/null)"
  if [ -n "$MATCHES" ]; then
    HIT=1
    HIT_COMMITS="$(echo "$MATCHES" | cut -d: -f1 | sort -u)"
    for term in "${DENY_WORD[@]}"; do
      term_matches="$(echo "$HIT_COMMITS" | xargs -I{} git --no-pager grep --no-color -i -w -F -l -e "$term" {} -- 2>/dev/null)"
      if [ -n "$term_matches" ]; then
        while IFS= read -r m; do
          fail "denylist term (whole word) '$term' found in $m"
        done <<< "$term_matches"
      fi
    done
  fi
fi

if [ "${#DENY_WORD_CS[@]}" -gt 0 ]; then
  WORD_CS_PATFILE="$(mktemp)"
  printf '%s\n' "${DENY_WORD_CS[@]}" > "$WORD_CS_PATFILE"

  MATCHES="$(git --no-pager grep --no-color -w -F -l -f "$WORD_CS_PATFILE" "${COMMIT_ARR[@]}" -- 2>/dev/null)"
  if [ -n "$MATCHES" ]; then
    HIT=1
    HIT_COMMITS="$(echo "$MATCHES" | cut -d: -f1 | sort -u)"
    for term in "${DENY_WORD_CS[@]}"; do
      term_matches="$(echo "$HIT_COMMITS" | xargs -I{} git --no-pager grep --no-color -w -F -l -e "$term" {} -- 2>/dev/null)"
      if [ -n "$term_matches" ]; then
        while IFS= read -r m; do
          fail "denylist term (case-sensitive whole word) '$term' found in $m"
        done <<< "$term_matches"
      fi
    done
  fi
fi

EMAIL_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' {} -- 2>/dev/null \
  | grep -v -E 'noreply@anthropic\.com|users\.noreply\.github\.com|@anthropic\.com|console\.anthropic\.com|example\.com')"
if [ -n "$EMAIL_HITS" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "possible email address in $m"
  done <<< "$EMAIL_HITS"
fi

GPS_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '[0-9]{1,3}\.[0-9]{4,},[[:space:]]*-?[0-9]{1,3}\.[0-9]{4,}' {} -- 2>/dev/null)"
if [ -n "$GPS_HITS" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "possible GPS coordinate pair in $m"
  done <<< "$GPS_HITS"
fi

# No \b here -- git grep -E is POSIX ERE, which doesn't support \b (it
# silently matches nothing, rather than erroring, so this is easy to get
# wrong without testing). The pattern is specific enough without it.
SSN_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '[0-9]{3}-[0-9]{2}-[0-9]{4}' {} -- 2>/dev/null)"
if [ -n "$SSN_HITS" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "SSN-like number in $m"
  done <<< "$SSN_HITS"
fi

# UK National Insurance number: two letters (excluding D,F,I,Q,U,V as first
# letter and D,F,I,O,Q,U,V as second -- simplified here to the common
# subset used in practice), six digits, one letter A-D.
NINO_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '[A-CEGHJ-PR-TW-Z]{2}[0-9]{6}[A-D]' {} -- 2>/dev/null)"
if [ -n "$NINO_HITS" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "UK National Insurance-number-like value in $m"
  done <<< "$NINO_HITS"
fi

# UK postcode. The space between outward and inward code is REQUIRED here,
# not optional -- an optional space matches hex colours and git-hash-
# derived filenames instead of postcodes.
POSTCODE_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '[A-Za-z]{1,2}[0-9][0-9A-Za-z]? [0-9][A-Za-z]{2}' {} -- 2>/dev/null)"
if [ -n "$POSTCODE_HITS" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "UK-postcode-like value in $m"
  done <<< "$POSTCODE_HITS"
fi

# UK sort code (NN-NN-NN) -- WARN not FAIL. This shape collides with plain
# dates, so it can't be a hard FAIL without blocking unrelated pushes.
SORTCODE_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '[0-9]{2}-[0-9]{2}-[0-9]{2}' {} -- 2>/dev/null)"
if [ -n "$SORTCODE_HITS" ]; then
  while IFS= read -r m; do
    warn "possible UK sort code (or just a date -- this shape collides) in $m"
  done <<< "$SORTCODE_HITS"
fi

# UK mobile number -- WARN not FAIL, formatting varies too much for a hard
# FAIL (spaces, +44 vs 0).
MOBILE_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '(\+44[ -]?7[0-9]{3}|\(?07[0-9]{3}\)?)[ -]?[0-9]{3}[ -]?[0-9]{3}' {} -- 2>/dev/null)"
if [ -n "$MOBILE_HITS" ]; then
  while IFS= read -r m; do
    warn "possible UK mobile number in $m"
  done <<< "$MOBILE_HITS"
fi

# Non-private IPv4 addresses -- WARN not FAIL, since a legitimate public
# endpoint (an API host, a documentation example) can trigger this
# harmlessly. Excludes RFC1918 private ranges, loopback, link-local, and
# multicast. Checks the actual matched IP (the text after the last ':' in
# git grep's tree:file:line:match output), not the whole line.
IP_RAW="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -noE '([0-9]{1,3}\.){3}[0-9]{1,3}' {} -- 2>/dev/null)"
IP_HITS=""
if [ -n "$IP_RAW" ]; then
  while IFS= read -r line; do
    ip="${line##*:}"
    case "$ip" in
      10.*|172.16.*|172.17.*|172.18.*|172.19.*|172.2[0-9].*|172.30.*|172.31.*|192.168.*|127.*|169.254.*|0.0.0.0|22[4-9].*|23[0-9].*)
        ;;
      *)
        IP_HITS="${IP_HITS}${IP_HITS:+$'\n'}${line}"
        ;;
    esac
  done <<< "$IP_RAW"
fi
if [ -n "$IP_HITS" ]; then
  while IFS= read -r m; do
    warn "non-private-looking IP address in $m -- confirm it's a legitimate public endpoint, not a real home IP"
  done <<< "$IP_HITS"
fi

# Credentials by known FORMAT -- FAIL. OAuth client secrets (GOCSPX-, what
# Google issues for the credentials.json this tool needs), Google API keys,
# refresh tokens as stored in token_*.json, and the common vendor prefixes
# (Anthropic, OpenAI, AWS, GitHub, Slack) plus PEM private-key headers.
# Ported from a sibling project's copy of this script.
#
# Unlike every PII rule above, this DELIBERATELY DOES NOT ECHO THE MATCH: a
# leaked credential printed into terminal scrollback or a CI log is a
# second copy of the thing being contained, so `git grep -l` lists only the
# commit:path. (The PII rules echo on purpose -- the matched text is the
# thing that shouldn't be there, and seeing it is how you find it. For a
# secret the location is enough to act on.) `-e` guards the leading dash of
# the PRIVATE KEY alternative from being read as an option.
SECRET_RE='GOCSPX-[A-Za-z0-9_-]{20,}|"refresh_token": *"[A-Za-z0-9_/-]{20,}|"client_secret": *"[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9_-]{30,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
SECRET_HITS="$(echo "$COMMITS" | xargs -I{} git --no-pager grep --no-color -lE -e "$SECRET_RE" {} -- 2>/dev/null)"
if [ -n "$SECRET_HITS" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "credential matching a known key format in $m -- value withheld; revoke it (Google Cloud console for an OAuth client, or remove the account's access at myaccount.google.com/permissions for a token) and purge from history"
  done <<< "$SECRET_HITS"
fi

# The OAuth artefacts tracked at all -- FAIL. .gitignore is the only thing
# keeping credentials.json and the two token files out of git, and
# `git add -f` (or an edit to .gitignore) silently defeats it. Matches the
# bare names at any depth; nothing legitimately tracked has these names.
SECRET_FILES_TRACKED="$(echo "$COMMITS" | xargs -I{} git --no-pager ls-tree -r --name-only {} 2>/dev/null \
  | grep -E '(^|/)(credentials\.json|client_secret[^/]*\.json|token_[^/]*\.json|\.pii-denylist)$' | sort -u)"
if [ -n "$SECRET_FILES_TRACKED" ]; then
  HIT=1
  while IFS= read -r m; do
    fail "a secrets file is tracked in git: $m -- it must stay gitignored (git rm --cached, then purge from history; revoke the OAuth client/token it held)"
  done <<< "$SECRET_FILES_TRACKED"
fi

# Office/archive files added anywhere in range -- WARN not FAIL. Git can't
# grep inside a .xlsx/.docx/.pdf/.zip/etc (they're zips or binary
# containers), so none of the checks above can see what's in one. This
# repo already gitignores a real .xlsx of label data for exactly this
# reason -- this is a nudge to check any *new* one by hand, not a
# substitute for that.
BINARY_HITS="$(echo "$COMMITS" | xargs -I{} git diff-tree --no-commit-id --name-status -r {} 2>/dev/null \
  | awk '$1 == "A" {print $2}' \
  | grep -iE '\.(xlsx|xls|docx|doc|pdf|zip|key|numbers|pages)$')"
if [ -n "$BINARY_HITS" ]; then
  while IFS= read -r m; do
    warn "office/archive file added: $m -- git can't see inside this, check it by hand"
  done <<< "$BINARY_HITS"
fi

if [ "$HIT" -eq 0 ]; then
  ok "$LABEL: clean"
fi

echo
echo "== Summary =="
echo "  $FAILS FAIL(s)"
if [ "$FAILS" -gt 0 ]; then
  exit 1
fi
exit 0
