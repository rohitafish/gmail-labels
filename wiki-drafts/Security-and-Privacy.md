# Security and Privacy

This page is the "why" behind this project's privacy and security controls.
For how to report a vulnerability, see
[SECURITY.md](https://github.com/rohitafish/gmail-labels/blob/main/SECURITY.md)
instead — that's the "how to report," this is the design.

## The two-scope OAuth split

`audit.py` and `apply_moves.py` authenticate separately, on separate OAuth
scopes, with separate token files:

| Script | Scope | Can do | Cannot do |
|---|---|---|---|
| `audit.py` | `gmail.readonly` | Read mail, to find each label's most recent message | Change a single thing in your account |
| `apply_moves.py` | `gmail.labels` | Create/rename labels | Read a single email |

This isn't a convention that could be violated by a bug elsewhere in the
code — it's enforced by Google's OAuth consent screen at the scope level.
Even if `apply_moves.py` had a defect that tried to read a message, the
access token it holds is physically incapable of authorizing that call.

## Nothing runs anywhere but your own machine

There's no server, nothing listens on a network port, and nothing is ever
sent anywhere except Google's own Gmail API over the same OAuth mechanism
Gmail's own official apps use. Running the scripts is the only way anything
happens — there's no background process, no scheduled task, and no telemetry.

## What's gitignored, and why

Everything that could identify you, your mailbox, or grant access to your
account never gets committed:

- `credentials.json`, `token_readonly.json`, `token_labels.json` — your
  OAuth client secret and access/refresh tokens.
- `label_audit.csv`, `journeys_audit.csv`, `apply_log*.csv`, `undo-*.csv` —
  every one of these contains your real label names.
- `.audit_cache.json` — cached real last-email dates per label.
- Any real reference spreadsheet used for one-off local verification.
- `.pii-denylist` — see below.

## The PII guardrail

`scripts/check-pii.sh` scans commits — and, with `--full`, this repo's
entire history — for known real values (from a local, gitignored
`.pii-denylist`) plus generic structural patterns (email addresses, GPS
coordinates, non-private IPs, SSN-like numbers, UK National Insurance
numbers and postcodes) as defense in depth. It's wired in two places:

- A **pre-push hook** (`scripts/hooks/pre-push`, copied into `.git/hooks/`
  once per clone) that also runs the test suite, coverage floor, and
  `ruff check` — a change that would leak PII, break tests, or fail lint
  is stopped before it ever leaves your machine.
- **CI**, as a server-side backstop for anything that reaches GitHub anyway.

`.pii-denylist` is the one sanctioned place real values (a real label name, a
real contact) live in plaintext — gitignored, per-machine, never committed,
same idea as `credentials.json` for secrets. The one thing no tooling can
catch: a **brand-new** real name used for the first time as an example isn't
on any denylist yet, and is indistinguishable from any other word. See
[CONTRIBUTING.md](https://github.com/rohitafish/gmail-labels/blob/main/CONTRIBUTING.md)'s
privacy section for the rule that covers that gap — never use a real label
name, bank, or contact as an example, anywhere, ever.

## The wiki you're reading right now

This page (and every other wiki page) is drafted in `wiki-drafts/` inside the
main repo, and published only via `./scripts/publish-wiki.sh` — never by
hand-editing the live wiki. That script runs the same PII scan before ever
pushing anywhere, refuses to run if the drafts have uncommitted changes, and
reads its git commit identity from this repo's own local config rather than
letting a throwaway clone of the wiki repo silently auto-detect one.
