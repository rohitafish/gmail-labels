# Security Policy

## Scope and context

This is a local command-line tool that talks to the Gmail API via OAuth. It
never runs a server, never listens on a network port, and is not exposed to
the internet in any way — everything happens as a script invoked by hand on
your own machine. `audit.py` runs on a read-only OAuth scope
(`gmail.readonly`) and cannot change anything in your account; `apply_moves.py`
runs on a separate, labels-only scope (`gmail.labels`) and cannot read a
single email. See [SETUP-LOCAL.md](SETUP-LOCAL.md) for the full picture.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report privately through GitHub's [**Report a vulnerability**](https://github.com/rohitafish/gmail-labels/security/policy)
button (under this repository's **Security** tab, if you're navigating there
directly), which opens a private security advisory visible only to the
maintainer. Include:

- what the issue is and where in the code it lives,
- how to reproduce it (a minimal case is ideal),
- the impact you think it has, and
- any suggested fix.

This is a personal, unfunded project maintained on a best-effort basis:

- **No bug bounty.** There is no monetary reward.
- **Acknowledgement** of a valid report within about a week.
- **Fixes** are prioritised by severity.

Please give a reasonable window to address an issue before disclosing it
publicly.

## Supported versions

Only the latest commit on the default branch is supported. There are no
long-lived release branches; fixes land on `main`.

## Handling of secrets and personal data

If a report involves a leaked OAuth artifact (`credentials.json`, a
`token_*.json` file) or personal data (real label names, email addresses) in
the code or git history, say so explicitly and **do not quote the value** in
the advisory — point to the file and location instead. The repository ships
`scripts/check-pii.sh` as a pre-push guard against exactly this class of
leak; if you find a gap in it, that itself is worth reporting.
