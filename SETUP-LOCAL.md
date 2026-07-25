# Gmail "Old" label tidy-up — local script setup

Renaming a Gmail label never touches the emails on it. Nothing in this process
deletes mail, and every rename is reversible by renaming back.

The audit and the apply step use **separate OAuth scopes and separate token
files**, on purpose:

| Step | Scope | Can do | Cannot do |
|---|---|---|---|
| `audit.py` | `gmail.readonly` | read mail | change anything at all |
| `apply_moves.py` | `gmail.labels` | create/rename labels | read a single email |

So the audit is incapable of mutating your account, and the apply step is
incapable of reading your mail.

---

## 1. Create an OAuth client (one-off, ~10 minutes)

This is the only part I can't do for you — it needs your Google account.

1. Go to **https://console.cloud.google.com/projectcreate**.
   Name it something like `gmail-label-tidyup`.

   **Organisation:** leave it as *No organization* — that's the default and the
   correct answer for a personal Google account. If you're on Google Workspace
   the field is instead required and pre-filled with your org; take it. The
   choice only affects which audience options step 3 offers.

   Create, then wait for it to become the selected project (top-left picker).

   Whichever Google account you create this under, the account you **authorise
   with** later must be the one holding the labels. They can differ, as long as
   the mailbox account is listed as a test user in step 4.

2. **Enable the Gmail API.** Go to
   **https://console.cloud.google.com/apis/library/gmail.googleapis.com**
   and click **Enable**.

3. **Configure the consent screen.** Go to
   **https://console.cloud.google.com/auth/overview**.
   - Click **Get started**.
   - App name: anything. User support email: your own address.
   - Audience: **External**. (**Internal** appears only for projects inside a
     Workspace org; take it if you see it — no warning screen, no test-user
     list, and tokens that last until revoked.)
   - Contact email: your own address. Agree, and **Create**.

4. **External only — add yourself as a test user.** Skip this step entirely if
   you chose Internal.

   Go to **https://console.cloud.google.com/auth/audience**, and under
   *Test users* click **Add users**, enter your own Gmail address, **Save**.

   This keeps the app in Testing mode — private to you, no Google verification
   needed. The catch is that refresh tokens expire after 7 days in that mode;
   if a run ever fails with `invalid_grant`, delete `token_readonly.json` /
   `token_labels.json` and re-authorise.

5. **Create the client.** Go to
   **https://console.cloud.google.com/auth/clients**, click
   **Create client**.
   - Application type: **Desktop app**
   - Name: anything
   - **Create**, then **Download JSON**.

6. Save that file into this folder as exactly **`credentials.json`**:

   ```
   ~/gmail_labels/credentials.json
   ```

`credentials.json`, `token_readonly.json` and `token_labels.json` are all
gitignored. Don't commit or share them.

---

## 2. Run the audit — this changes nothing

```bash
cd ~/gmail_labels && ./run audit.py
```

The first run opens a browser to authorise. If you set the audience to
**External**, Google will warn that the app isn't verified — click
**Advanced → Go to (unsafe)**. That warning appears for any app in Testing
mode, including your own. **Internal** apps don't show it.

It examines 517 in-scope labels (~1,000 API calls, roughly 3–4 minutes) and
writes **`label_audit.csv`**. Results are cached in `.audit_cache.json`, so
re-running is instant. Use `--refresh` to re-read everything from Gmail.

### The CSV

| Column | Meaning |
|---|---|
| LABEL | Current full label path |
| LABEL_ID | Gmail's internal id — leave alone, the apply step uses it |
| LAST_EMAIL | Date of the most recent email carrying that label |
| AGE_DAYS | How long since that email |
| MESSAGES | Approximate message count |
| LEAF | Whether it has sub-labels of its own |
| VERDICT | see the table below |
| PROPOSED_NEW_NAME | Where it would move to |
| **ACTION** | **MOVE or SKIP — edit this column to overrule** |
| NOTE | Why something was skipped |

Rows are sorted oldest-first. Open it in Excel or Sheets, edit the ACTION
column, save as CSV.

### Verdicts

| Verdict | Meaning |
|---|---|
| `ACTIVE` | Mail within the last 2 years. Left alone. |
| `STALE` | No mail for 2 years. Proposed for archiving. |
| `EMPTY` | No emails ever. Delete it by hand; the scripts never do. |
| `ARCHIVED` | Already under an `Old`, and still quiet. Correctly filed. |
| **`REVIVE`** | **Already under an `Old`, but mail has started arriving again. Proposed to come back up a level.** |
| `ARCHIVE CONTAINER` | The `Old` folder itself. Never moved in either direction. |
| `… (has sub-labels)` | Has children, so it is never renamed — that would orphan them. Its children are handled individually. |

### Archiving and revival

The audit runs in both directions. A stale label moves *down* into `Old`; an
archived label that starts receiving mail again moves *back up*, by dropping
the archive segment nearest the leaf:

```
Dosh 💹/Banks/Acme Bank        ->  Dosh 💹/Banks/Old/Acme Bank       (archive)
Dosh 💹/Banks/Old/Acme Bank    ->  Dosh 💹/Banks/Acme Bank           (revive)
Computing 👾/Old/Vendor/Reseller -> Computing 👾/Vendor/Reseller      (revive, deep)
```

These are exact inverses — verified against all 228 real moves from the first
run. Revival can only be triggered by genuinely new mail, since ageing alone
only ever pushes a label further past the line, so labels cannot oscillate
between runs.

Pass `--no-revive` to skip archived labels entirely and tidy one-way only.
That is faster, since checking for revivals roughly doubles the labels
examined (583 rather than 288 after the first run).

**Borderline rows** — anything within 183 days either side of the 2-year line —
are flagged in NOTE and defaulted to **SKIP**, whichever side they fall on.
They already carry a PROPOSED_NEW_NAME, so changing ACTION to `MOVE` is all
that's needed to include one.

**Empty labels** are reported as `EMPTY` and never moved. They're clutter
rather than archive, so delete them from Gmail's Settings → Labels page.

The audit also prints any category where *every* child came out stale, in case
you'd rather move the whole branch as a unit. Nothing is collapsed
automatically.

---

## 3. Apply

```bash
cd ~/gmail_labels && ./run apply_moves.py
```

Dry run by default — it prints every rename and every container it would
create, and changes nothing. Read the output.

When you're happy:

```bash
cd ~/gmail_labels && ./run apply_moves.py --apply
```

This authorises a second time, for the `gmail.labels` scope. It processes
deepest paths first so a parent is never renamed out from under a child, skips
any target name that already exists rather than crashing, and sleeps 120ms
between calls to stay inside the rate limits.

Every result is written to a timestamped **`apply_log-<date>-<time>.csv`** with
the original name in column 1. Dry runs get a `-dryrun` suffix. Nothing is ever
overwritten — those logs are the only record of the old names, so keep them.

Gmail's sidebar can take a minute to catch up. Reload Gmail if the tree looks
wrong immediately afterwards.

### Undoing

Swap the first two columns of the apply log, rename the headers to `LABEL`
and `PROPOSED_NEW_NAME`, add an `ACTION` column of `MOVE`, and run
`apply_moves.py --csv thatfile.csv`. The run writes its own fresh timestamped
log, so the file you are reversing from is never touched.

For a single label that was archived by mistake, you don't need any of this —
just wait for the next audit. If mail has arrived on it, it comes back as a
`REVIVE` row automatically.

---

## 4. Verification (do this before the full apply)

```bash
cd ~/gmail_labels && ./run audit.py --only Journeys --out journeys_audit.csv
cd ~/gmail_labels && ./run verify_journeys.py journeys_audit.csv
```

This diffs a fresh Journeys audit against your hand-checked
`Gmail label audit - Journeys.xlsx` and reports coverage gaps, verdict
mismatches and date differences.

Two things to expect:

- **`Journeys✈️/Car/Taxis/RideCo` will show as missing from the spreadsheet.**
  It's a real, unarchived leaf with 64 threads that the earlier audit didn't
  cover. The correct leaf count for Journeys is 62, not 61.
- **Small date differences are normal.** The spreadsheet used thread dates
  (the last message of the newest thread carrying the label — that message may
  not itself carry the label). This audit uses message dates: the newest
  message that actually has the label on it. That's stricter and more correct,
  but it can read a little older.

Verdict mismatches are the ones that matter. Investigate those before applying
at scale.

---

## Tuning

Settings live at the top of `gmail_common.py`:

```python
SCOPE_PREFIXES  = ['Dosh', 'Politics', 'Business', 'Computing', 'Journeys', 'Friends']
STALE_YEARS     = 2
BORDERLINE_DAYS = 183
```

`SCOPE_PREFIXES` is a plain "starts with" match, so the emoji and their
invisible variation selectors never have to be typed. Set it to `[]` to process
every top-level label — but note that if you do, the top-level `zOld 💾` label
won't be recognised as an archive, because the segment test matches `zold`
exactly and that name carries an emoji. Add it to `ARCHIVE_SEGMENTS` first.
