# Troubleshooting / FAQ

A short list of the things that have actually come up. If something else
goes wrong, [open an issue](https://github.com/rohitafish/gmail-labels/issues)
with the exact command and output.

**A run suddenly asks me to sign in again, or opens a browser unexpectedly.**
Your OAuth token expired and couldn't be silently refreshed — this happens on
its own eventually, and immediately if Google's consent screen for this app
is still in "Testing" publishing status, where refresh tokens expire after 7
days of inactivity. Complete the browser consent screen again; nothing about
your data or the audit logic is affected.

---

**`apply_moves.py` reported a `FAILED` row instead of `DONE`.** Check the
`ERROR` column of the timestamped `apply_log-*.csv` it wrote. A `409` usually
means the target name was created by something else between the audit and
this run — re-run `audit.py` to get a fresh, accurate plan. Any other error
is worth reading literally; `apply_moves.py` doesn't stop the whole run for
one failed rename, so check every `FAILED` row, not just the first.

---

**A label I expected to see as `REVIVE` shows `ARCHIVED` instead.** Revival
requires mail within `REVIVE_MONTHS` (6 months by default) — a much tighter
bar than the 2-year line that archived it in the first place. A label with,
say, 8-month-old mail correctly stays `ARCHIVED` under this rule; see
[How the Audit Works](How-the-Audit-Works) for the full reasoning. If the
label is a leaf and the mail is genuinely more recent than that, check
whether the label has sub-labels (`LEAF` column) — non-leaf labels are never
proposed for anything, only reported.

---

**The first run against a large mailbox is slow.** Every label with no cache
entry needs a fresh Gmail API lookup, paced with a small delay to stay
inside rate limits. A first run over several hundred labels can take a
few minutes; subsequent runs within `CACHE_MAX_AGE_DAYS` (7 days by default)
reuse `.audit_cache.json` and are close to instant. Pass `--no-revive` to
skip archived labels entirely if you only need a one-way tidy — checking for
revivals roughly doubles the labels examined.

---

**I want to undo a batch of moves.** Take the `apply_log-*.csv` from the run
you want to reverse, swap its first two columns, rename the headers to
`LABEL` and `PROPOSED_NEW_NAME`, add an `ACTION` column of `MOVE` to every
row, and run `apply_moves.py --csv thatfile.csv`. The run writes its own
fresh, separately timestamped log, so the file you're reversing from is
never touched or overwritten.

---

**A single label I didn't mean to archive got moved by mistake.** You don't
need the full undo procedure above — just wait for the next audit. If mail
has arrived on it since, it comes back automatically as a `REVIVE` row (once
within the 6-month window) without any manual intervention.

---

**Still stuck?** Open an issue with the exact command you ran and its full
output (redact any real label names first — see
[Security and Privacy](Security-and-Privacy) for why that matters).
