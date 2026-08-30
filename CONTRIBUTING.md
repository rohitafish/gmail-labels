# Contributing

Thanks for your interest. This is a personal Gmail label tidy-up toolkit,
but issues and pull requests are welcome. A few things are specific to this
repo -- the privacy guard in particular -- so please read this before your
first commit.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

That's enough to run the test suite. Running the scripts themselves against
a real Gmail account needs an OAuth client -- see **SETUP-LOCAL.md** for
that one-off setup. None of it is needed just to develop or test a change:
the suite runs entirely against a hand-rolled fake Gmail service
(`tests/conftest.py`), never a real account, network call, or
`credentials.json`.

## Privacy: read this first

- **`scripts/check-pii.sh`** scans the commits you're about to push -- and,
  with `--full`, all of history -- for known real values and structural PII
  (emails, GPS coordinates, non-private IPs, SSN-like numbers, UK National
  Insurance numbers and postcodes).
- **`.pii-denylist`** (repo root, gitignored, per-machine) is where exact
  real values live -- one literal string per line, same idea as
  `credentials.json`/the token files for secrets. It is never committed. A
  fresh clone starts without it; the structural pattern checks still run.
- **Install the pre-push hook once per clone** (git does not clone hooks):

  ```bash
  cp scripts/hooks/pre-push .git/hooks/pre-push
  chmod +x .git/hooks/pre-push
  ```

  It blocks any push that fails the tests, drops coverage below the floor,
  fails `ruff check`, or trips `check-pii.sh`.

### The one rule the tooling can't enforce

**Never use a real label name, bank, or contact as an example** -- not in
code, comments, test fixtures, or commit messages. A brand-new real name
isn't on any denylist yet and is indistinguishable from any other word, so
no check can catch it. Use obviously fabricated values (the test suite's own
`Widgets 🧩/Vendors/Acme Supply`-shaped names are the convention). If you
learn of a real value that must never reappear, add it to your local
`.pii-denylist`.

## Tests and linting

```bash
.venv/bin/coverage run -m pytest -q
.venv/bin/coverage report
.venv/bin/ruff check .
```

(Plain `.venv/bin/pytest` also works if you just want the tests without the
coverage gate.) All must pass; the pre-push hook runs them too. Please add
or update tests for behaviour you change -- the suite is thorough,
including a dedicated `tests/test_check_pii.py` for the privacy guard
itself and `tests/test_docs_and_guardrails.py` for keeping SETUP-LOCAL.md
in sync with the code.

CI also enforces a **coverage floor** -- see `.coveragerc`'s `fail_under`.
It's a ratchet, not a target: if your change legitimately can't reach it,
that's a normal PR conversation, not something to work around by padding
coverage elsewhere.

Patterns you'll reuse when adding tests:

- **`fake_service` fixture** (`tests/conftest.py`) -- a hand-rolled fake
  Gmail service backing `labels().list/create/patch` and
  `messages().list/get`. No mocking library is used anywhere in this suite;
  match that style rather than introducing one.
- **`make_label`/`make_cache_entry`/`make_row`** -- plain factory functions
  (not fixtures) for building test data in the shapes `gmail_common.py` and
  `audit.py` actually use.
- Every label name in a test is fabricated, in the `Widgets 🧩/...` shape --
  see the privacy note above.

## Code style

`ruff check .` enforces this repo's pinned lint rules -- see `ruff.toml` for
exactly which, and why each deliberately-omitted family is left out. There's
no separate auto-formatter step; a passing `ruff check` is the bar.

Beyond that, match the codebase's existing style: comments explain *why*,
not just *what*, especially for a non-obvious decision.

## Documentation

Deeper docs than this file or the README carry live in the
[wiki](https://github.com/rohitafish/gmail-labels/wiki), sourced from
`wiki-drafts/*.md` in this repo. That directory is the editable source —
publish changes only via `./scripts/publish-wiki.sh`, never by hand-editing
the live wiki. See that script's header comment for why (it reads this
repo's own git identity, refuses to run on uncommitted drafts, and runs
`check-pii.sh` explicitly before ever pushing anywhere public).

## Submitting a change

This repo only has one collaborator with push access, so every external
contribution goes through a fork:

1. **Fork** the repo on GitHub, then clone your fork locally.
2. Create a branch for your change (`git checkout -b my-change`).
3. Make the change, following the code style above.
4. Run the checks locally: `coverage run -m pytest -q && coverage report`,
   `ruff check .`, and -- if you have `.pii-denylist` populated, which a
   fresh clone won't -- `scripts/check-pii.sh`.
5. Commit with a clear message (remember: **messages are scanned and become
   public** -- no real personal data in them) and push to *your fork*.
6. Open a pull request against this repo's `main` branch.

CI (`ruff`, `pytest` under `coverage` with its floor enforced, and the
PII/secret scan, matrixed across Python 3.12 and 3.14) runs automatically on
your PR and must pass before it can be merged.

For security vulnerabilities, do **not** open a public issue or PR — see
[SECURITY.md](SECURITY.md).
