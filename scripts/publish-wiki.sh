#!/usr/bin/env bash
set -euo pipefail
# Publishes wiki-drafts/*.md to the GitHub Wiki --
# https://github.com/rohitafish/gmail-labels.wiki.git, a SEPARATE git repo
# from this one -- not part of this repo's history or working tree.
#
# Always publish through this script -- never clone/copy/push to the wiki
# by hand. A sibling project (home-asset-manager) did exactly that once and
# leaked the operator's real name and hostname into a public wiki commit: a
# throwaway `git clone` of the wiki repo has no local git identity of its
# own, so it silently falls back to git's username+hostname auto-detection
# instead of erroring. Every step below exists to make that specific mistake
# structurally impossible rather than something to remember not to do.
#
# Usage:
#   ./scripts/publish-wiki.sh              # publish every changed page
#   ./scripts/publish-wiki.sh Home.md ...  # publish only the named page(s)
#
# WIKI_REPO_URL overrides the target, for testing this script itself.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRAFTS_DIR="$REPO_DIR/wiki-drafts"
WIKI_REPO_URL="${WIKI_REPO_URL:-https://github.com/rohitafish/gmail-labels.wiki.git}"

cd "$REPO_DIR"

# ---- 1. wiki-drafts must be clean ----
# What gets published is content that has already been committed (and
# therefore already scanned by the pre-push hook and CI) -- not whatever
# happens to be sitting in the working tree right now.
if [ -n "$(git status --porcelain -- wiki-drafts 2>/dev/null)" ]; then
  echo "wiki-drafts/ has uncommitted changes -- commit or stash them first." >&2
  echo "(This check alone isn't the real gate -- see step 2.)" >&2
  exit 1
fi

# ---- 2. the real gate: run the PII/secret scanner explicitly ----
# The pre-push hook and CI don't fire for a commit that's clean locally but
# was never pushed to this repo's own origin -- this is what actually
# stands between a bad draft and the public wiki.
echo "Running scripts/check-pii.sh --full before publishing..."
if ! "$REPO_DIR/scripts/check-pii.sh" --full; then
  echo "check-pii.sh found something above -- refusing to publish." >&2
  exit 1
fi

# ---- 3. read OUR identity from THIS repo's own local git config ----
# Never let the wiki clone fall back to auto-detection. Empty is a hard
# refusal, not a warning.
GIT_NAME="$(git -C "$REPO_DIR" config user.name || true)"
GIT_EMAIL="$(git -C "$REPO_DIR" config user.email || true)"
if [ -z "$GIT_NAME" ] || [ -z "$GIT_EMAIL" ]; then
  echo "This repo's own git user.name/user.email isn't set locally -- refusing" >&2
  echo "to publish rather than let the wiki clone fall back to an auto-detected" >&2
  echo "identity (name+hostname). Set them with:" >&2
  echo "  git config user.name  '...'" >&2
  echo "  git config user.email '...'" >&2
  exit 1
fi

# Signing config, if this repo has it configured -- carried over so wiki
# commits are signed the same way this repo's own commits are. Optional:
# the wiki repo carries no require-signed-commits ruleset of its own.
GIT_SIGNINGKEY="$(git -C "$REPO_DIR" config user.signingkey || true)"
GIT_GPGFORMAT="$(git -C "$REPO_DIR" config gpg.format || true)"
GIT_GPGSIGN="$(git -C "$REPO_DIR" config commit.gpgsign || true)"
GIT_ALLOWED_SIGNERS="$(git -C "$REPO_DIR" config gpg.ssh.allowedSignersFile || true)"

# ---- 4. build the page list ----
# Explicit args, or every *.md in wiki-drafts. Only .md -- images/ is never
# copied by this script; screenshots go up some other way if this project
# ever has any.
PAGES=()
if [ "$#" -gt 0 ]; then
  PAGES=("$@")
else
  for f in "$DRAFTS_DIR"/*.md; do
    [ -e "$f" ] && PAGES+=("$(basename "$f")")
  done
fi
if [ "${#PAGES[@]}" -eq 0 ]; then
  echo "No pages to publish -- wiki-drafts/ has no .md files." >&2
  exit 1
fi

# ---- 5. clone the wiki repo into a scratch dir ----
SCRATCH="$(mktemp -d)"
cleanup() { rm -rf "$SCRATCH"; }
trap cleanup EXIT

echo "Cloning $WIKI_REPO_URL ..."
if ! git clone -q "$WIKI_REPO_URL" "$SCRATCH/wiki" 2>/tmp/publish-wiki-clone-err.$$; then
  echo "" >&2
  echo "Failed to clone the wiki repo." >&2
  if grep -qi "not found\|repository not found\|does not exist" /tmp/publish-wiki-clone-err.$$ 2>/dev/null; then
    echo "" >&2
    echo "This usually means the wiki hasn't been initialized yet -- GitHub only" >&2
    echo "creates <repo>.wiki.git once the wiki has at least one page. One-time" >&2
    echo "fix: open the repo's Wiki tab on github.com and click 'Create the" >&2
    echo "first page' (any content, even a placeholder), then re-run this script." >&2
  fi
  rm -f /tmp/publish-wiki-clone-err.$$
  exit 1
fi
rm -f /tmp/publish-wiki-clone-err.$$

# ---- 6. symlink guard ----
# The wiki is public and third-party-editable via GitHub's own wiki editor.
# Both `diff` and `cp` below follow symlinks, so a malicious page checked in
# as a symlink could redirect a write outside the scratch dir. Refuse
# outright rather than special-case it.
if find "$SCRATCH/wiki" -type l ! -path "*/.git/*" | grep -q .; then
  echo "Refusing to proceed: the cloned wiki contains a symlink." >&2
  find "$SCRATCH/wiki" -type l ! -path "*/.git/*" >&2
  exit 1
fi

# ---- 7. set the scratch clone's identity explicitly ----
git -C "$SCRATCH/wiki" config user.name "$GIT_NAME"
git -C "$SCRATCH/wiki" config user.email "$GIT_EMAIL"
if [ -n "$GIT_SIGNINGKEY" ]; then
  git -C "$SCRATCH/wiki" config user.signingkey "$GIT_SIGNINGKEY"
  git -C "$SCRATCH/wiki" config gpg.format "$GIT_GPGFORMAT"
  git -C "$SCRATCH/wiki" config commit.gpgsign "$GIT_GPGSIGN"
  [ -n "$GIT_ALLOWED_SIGNERS" ] && \
    git -C "$SCRATCH/wiki" config gpg.ssh.allowedSignersFile "$GIT_ALLOWED_SIGNERS"
fi

# ---- 8. copy changed pages ----
CHANGED=()
for page in "${PAGES[@]}"; do
  src="$DRAFTS_DIR/$page"
  if [ ! -f "$src" ]; then
    echo "WARN: '$page' is not in wiki-drafts/ -- skipping (not creating a new page from a typo)." >&2
    continue
  fi
  dest="$SCRATCH/wiki/$page"
  if [ -f "$dest" ] && diff -q "$src" "$dest" >/dev/null 2>&1; then
    continue
  fi
  rm -f "$dest"   # never a bare overwrite -- always a fresh regular file
  cp "$src" "$dest"
  CHANGED+=("$page")
done

if [ "${#CHANGED[@]}" -eq 0 ]; then
  echo "Nothing to publish -- the wiki already matches wiki-drafts/."
  exit 0
fi

# ---- 9. show the diff and confirm ----
# Stage before diffing, not after confirming: a plain `git diff` shows
# nothing at all for a brand-new page (git diff only compares tracked
# content, and an untracked file has none to compare against) -- which
# would silently hide every new page from this review step while still
# showing modifications to existing ones. `git diff --cached` on the
# staged tree shows both correctly. Staging here is harmless even if the
# answer below is No -- SCRATCH is deleted by the EXIT trap regardless.
( cd "$SCRATCH/wiki" && git add -A -- "${CHANGED[@]}" && git --no-pager diff --cached -- "${CHANGED[@]}" )
echo ""
echo "This will publish immediately to the PUBLIC wiki -- no PR/review gate,"
echo "same as any other publish-to-a-public-place action."
read -r -p "Push the above to the wiki as ${GIT_NAME}? [y/N] " REPLY
case "$REPLY" in
  y|Y) ;;
  *) echo "Aborted -- nothing was pushed."; exit 1 ;;
esac

# ---- 10. commit and push ----
( cd "$SCRATCH/wiki" && git add -A -- "${CHANGED[@]}" \
  && git commit -q -m "Update ${CHANGED[*]}" \
  && git push -q )

# ---- 11. verify the pushed commit is actually attributed the way we asked ----
PUSHED_IDENTITY="$(cd "$SCRATCH/wiki" && git log -1 --format='%an <%ae>')"
EXPECTED_IDENTITY="$GIT_NAME <$GIT_EMAIL>"
if [ "$PUSHED_IDENTITY" != "$EXPECTED_IDENTITY" ]; then
  echo "" >&2
  echo "ERROR: the wiki has ALREADY been updated with the wrong identity." >&2
  echo "  expected: $EXPECTED_IDENTITY" >&2
  echo "  pushed:   $PUSHED_IDENTITY" >&2
  echo "This needs a corrected, force-pushed commit over it on the wiki repo --" >&2
  echo "GitHub can keep the original object reachable by its raw SHA for a" >&2
  echo "while afterward regardless. Fix the identity here first, then re-run." >&2
  exit 1
fi

echo "==> Done -- published as $PUSHED_IDENTITY: ${CHANGED[*]}"
