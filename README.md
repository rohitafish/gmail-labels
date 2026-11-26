# Gmail label tidy-up

**[Wiki](https://github.com/rohitafish/gmail-labels/wiki) · [Report a security issue](https://github.com/rohitafish/gmail-labels/security/policy)**

A local Python toolkit that tidies up a large Gmail label tree: dormant
labels get moved under category-level `Old` sub-labels, and archived labels
that start receiving mail again get moved back up. Nothing is ever deleted —
renaming a Gmail label preserves every email on it, and every move is
reversible by renaming back.

**Security-relevant facts up front:**

- **Two separate OAuth scopes, deliberately.** The audit step
  (`audit.py`) runs on `gmail.readonly` and is physically incapable of
  changing anything in your account. The apply step (`apply_moves.py`) runs
  on a separate `gmail.labels` scope and is physically incapable of reading
  a single email. They use separate token files too.
- **Everything runs locally.** There's no server, nothing listens on a
  network port, and nothing is ever sent anywhere except Google's own Gmail
  API. See [SECURITY.md](SECURITY.md).
- **Your real data never leaves your machine or gets committed.** OAuth
  credentials, token files, and every audit CSV containing your actual label
  names are gitignored. A pre-push hook and CI both scan for exactly this
  class of leak — see [SECURITY.md](SECURITY.md) and the wiki's
  [Security and Privacy](https://github.com/rohitafish/gmail-labels/wiki/Security-and-Privacy)
  page.

## One-time setup

You'll need a Google OAuth client to let the scripts talk to your own Gmail
account. The full walkthrough — creating the client, handling Google's
"unverified app" warning, and the first-run consent screens — is in
[SETUP-LOCAL.md](SETUP-LOCAL.md). Once that's done:

```bash
python3 -m venv .venv
./.venv/bin/pip install --require-hashes -r requirements.txt
```

## Running it

```bash
./run audit.py                  # phase 1: read-only, writes label_audit.csv
```

Open `label_audit.csv`, review the proposed moves, and edit the `ACTION`
column (`MOVE`/`SKIP`) for anything you want to override — see
[SETUP-LOCAL.md](SETUP-LOCAL.md#the-csv) for what each column and verdict
means, or the wiki's
[How the Audit Works](https://github.com/rohitafish/gmail-labels/wiki/How-the-Audit-Works)
for the full decision tree.

```bash
./run apply_moves.py            # phase 2: dry run by default, prints every rename
./run apply_moves.py --apply    # actually perform the renames
```

Every apply run writes a timestamped `apply_log-*.csv` — keep it, it's the
only record of the original names if you ever want to undo a move.

### Auditing filters

`filter_audit.py` works out which Gmail filters you can safely delete. It is
read-only and uses the same `gmail.readonly` token as `audit.py`.

1. In Gmail, go to Settings → Filters and Blocked Addresses, select all, and
   click **Export**. Save the file as `filters/mailFilters.xml`. That folder is
   gitignored, and the file is also your backup.
2. Run the audit. It searches for each filter's criteria across all mail,
   Spam and Trash included, and writes `filter_audit.csv`:

   ```bash
   ./run filter_audit.py --limit 20   # quick trial
   ./run filter_audit.py              # every filter
   ```

   | VERDICT | Meaning | Suggested ACTION |
   |---|---|---|
   | `ACTIVE` | matched mail in the last year | `KEEP` |
   | `QUIET` | last match 1–2 years ago | `REVIEW` |
   | `DEAD` / `NEVER MATCHED` | no match in 2 years, or ever | `DELETE` |
   | `DUPLICATE` | same criteria and actions as an earlier filter | `DELETE` |
   | `REDUNDANT` | a broader filter (e.g. `shop.example.com` for `news@shop.example.com`) already does the same | `DELETE` |
   | `NO ACTION` | does nothing to the mail it matches | `DELETE` |

   A dead filter that forwards, never-spams, trashes or marks important is
   suggested for `REVIEW` instead, because those actions can matter even
   without recent mail. Trashed mail is purged after 30 days, so a working
   delete filter can look unused.

3. Edit the `ACTION` column, then build a pruned export:

   ```bash
   ./run filter_audit.py --prune filter_audit.csv   # writes filters/mailFilters-pruned.xml
   ```

4. Gmail's Import adds filters but does not replace them. To apply the pruned
   file, delete every filter in Gmail, then Import `mailFilters-pruned.xml`.
   Keep the original export: importing it restores the old set.

## Tuning

The staleness/revival/scope rules live as plain constants at the top of
`gmail_common.py` (`SCOPE_PREFIXES`, `STALE_YEARS`, `BORDERLINE_DAYS`,
`REVIVE_MONTHS`, `ARCHIVE_SEGMENTS`, `CACHE_MAX_AGE_DAYS`). See
[SETUP-LOCAL.md](SETUP-LOCAL.md#tuning) for a quick reference, or the wiki's
[Configuration Reference](https://github.com/rohitafish/gmail-labels/wiki/Configuration-Reference)
for what each one does and the consequence of changing it.

## Development and testing

```bash
.venv/bin/pip install --require-hashes -r requirements.txt -r requirements-dev.txt
.venv/bin/coverage run -m pytest -q
.venv/bin/coverage report
.venv/bin/ruff check .
```

The test suite runs entirely against a hand-rolled fake Gmail service — no
real account, network call, or `credentials.json` is ever touched. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the full picture, including the
privacy rules for anyone submitting a change.

## Notes on scope

This is a personal tool built for one mailbox and one way of organising
labels (category-level `Old` sub-labels, leaves-only moves). It doesn't batch
across multiple accounts, doesn't expose any configuration beyond the
constants in `gmail_common.py`, and doesn't try to be a general-purpose
Gmail label manager — see the wiki's
[Home](https://github.com/rohitafish/gmail-labels/wiki) page for who this is
and isn't a good fit for.

## Contributing and security

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md), and read
its privacy section before your first commit. To report a security
vulnerability, use GitHub's
[**Report a vulnerability**](https://github.com/rohitafish/gmail-labels/security/policy)
button rather than opening a public issue — see [SECURITY.md](SECURITY.md)
for what to include.

For deeper documentation than this README carries, see the
[wiki](https://github.com/rohitafish/gmail-labels/wiki).

## Licence

Released under the [MIT Licence](LICENSE).
