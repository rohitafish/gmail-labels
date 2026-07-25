# Prompt for Claude Code

Copy everything below the line into Claude Code. Put `GmailOldLabels.gs` and
`Gmail label audit - Journeys.xlsx` in the working directory first so it can read them.

---

I want to tidy up my Gmail labels. I have ~1,090 labels and the tree has become hard to
navigate. The plan is to move dormant labels under `Old` sub-labels, keeping the emails intact.

## The rule

A label is **stale** if it has no email newer than **2 years**. A stale label moves by
**inserting `Old` immediately before its final segment**:

```
Dosh 💹/Banks/Acme Bank        ->  Dosh 💹/Banks/Old/Acme Bank
Journeys✈️/Rail/Acme Rail ->  Journeys✈️/Rail/Old/Acme Rail
Hobbies 🎲/Chess               ->  Health⚕️/Old/Pulse
```

This matches the pattern I'd already started by hand (`Business 🏭/Clients/Old/Northwind`,
`Hobbies 🎲/Old/Chess`), so `Old` sits at the category level, not the top level.

## Decisions already made — please don't relitigate these

- **Reuse existing archives.** Where `Old` or `zOld` already exists at the right level, write
  into it. Never create a second one.
- **Skip anything already archived.** Any label with `Old` or `zOld` as a path segment is left
  entirely alone (`Computing 👾/Old/*`, `Journeys✈️/Car/Old/*`, `Business 🏭/Clients/Old/*`).
- **Leaves only.** A label that has sub-labels is never moved, only its children are. Gmail
  stores each label's full path as its name, so renaming a parent orphans everything below it.
- **No branch collapsing.** Each stale leaf moves individually. Do report any category where
  *every* child came out stale, so I can decide whether to move the branch as a unit.
- **Empty labels** (zero emails ever) are reported for deletion, not moved.
- **Nothing is deleted.** Renaming preserves every email on a label.

## Scope

Six top-level parents, ~555 labels between them:
`Dosh 💹`, `Politics 🛠️`, `Business 🏭`, `Computing 👾`, `Journeys✈️`, `Friends & Family ䷤`

Match these by plain "starts with" on the text (`'Dosh'`, `'Politics'`, ...) so you never have
to reproduce the emoji or its variation selectors exactly. That matters — several of these
names contain invisible variation-selector characters.

**`Journeys✈️` is already audited** — results are in `Gmail label audit - Journeys.xlsx`
(61 leaves, 25 stale). Use it to sanity-check your output before trusting the rest. Five
parents remain.

## What I want

1. **An audit that changes nothing**, producing a reviewable table: label, date of most recent
   email, age in days, verdict, proposed new name, and an ACTION column I can edit.
2. **An apply step** that reads back my edited ACTIONs and performs only the renames I approved.
   Dry-run first, printing every rename it would make.
3. **Flag borderline cases** — anything within ~6 months either side of the 2-year line. In the
   Journeys audit, `Air/Skylark` came out stale by 48 days and `Air/Bluejet` active by 20. Those
   are judgement calls, not automation calls.

## Route — your call, but here's what I know

`GmailOldLabels.gs` in this directory is a working Google Apps Script that does all of the
above. Two ways to use it:

- **clasp** (`npm i -g @google/clasp`) to push it to my account and open it. Note `clasp run`
  needs a standard GCP project and the Apps Script API enabled, which may be more setup than
  it's worth — pushing and letting me click Run in the browser is fine.
- **Port it to a local script** (Python or Node) against the Gmail API with OAuth credentials.
  This is probably better: Apps Script caps execution at 6 minutes, so 555 labels means
  repeatedly clicking Run, whereas a local script just finishes. It also means you can run it,
  read the output, and iterate when something surprises us.

Tell me which you recommend and why before you start building.

## Technical gotchas already discovered — these cost me time, don't rediscover them

- **`GmailApp` has no rename.** It can create and delete labels but not rename. You need the
  advanced Gmail service / Gmail API: `Gmail.Users.Labels.patch({name: newName}, 'me', labelId)`.
  In Apps Script, add it via **Services → Gmail API**.
- **The Gmail API does not cascade renames to children.** Each label is a separate object whose
  name is its full path. Patching `Dosh 💹/Banks` does not touch `Dosh 💹/Banks/Acme Bank`. Always
  process deepest-path-first, and only move leaves.
- **Renaming to a name that already exists fails.** Check for collisions before patching and
  report them rather than crashing the run.
- **Gmail search by label name works fine with emoji and slashes** when quoted:
  `label:"Journeys✈️/Rail/Acme Rail"`. Results come back newest-first, so the first result
  gives you the last-email date directly. `GmailApp.getUserLabelByName(name).getThreads(0, 1)`
  works the same way and avoids search-syntax escaping entirely.
- **Rate limits.** Sleep ~120ms between patch calls.
- **Some threads carry surprising labels.** During the audit I hit a thread labelled
  `Hotels/BookingSite` whose sender was a newspaper, and one under `Air/FlightSearch` from
  a deals newsletter. Don't treat odd senders as bugs in your code — my labelling is just messy
  in places.

## Verification

Before I run the apply step against 500 labels, I want evidence the logic is right:

- Re-audit `Journeys✈️` and diff your verdicts against
  `Gmail label audit - Journeys.xlsx`. They should match on all 61 leaves. If they don't,
  work out which of us is wrong before going further.
- Show me the proposed rename for a 4-level label and a 2-level label, so I can confirm the
  `Old` insertion lands where I expect.
- Confirm your archived-label filter actually excludes `Computing 👾/Old/Vendor/Reseller`.
