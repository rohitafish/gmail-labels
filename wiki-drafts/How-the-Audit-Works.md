# How the Audit Works

This page narrates the full decision tree behind every `VERDICT` in
`label_audit.csv` — the "why," not just the column reference already in
[SETUP-LOCAL.md](https://github.com/rohitafish/gmail-labels/blob/main/SETUP-LOCAL.md#the-csv).
The actual implementation is `audit.classify()` in `audit.py`, a pure
function with no service or clock of its own — everything here is exactly
what that function does, not an approximation of it.

## The rule that shapes everything: leaves only

Gmail stores each label's full path as its name. `Dosh/Banks/Acme Bank` is
one label whose name happens to contain slashes — it isn't three nested
objects. Renaming `Dosh/Banks` to `Dosh/Old/Banks` does **not** touch
`Dosh/Banks/Acme Bank`, which would silently become an orphan with no visible
parent in the label tree. So: **a label with any sub-labels beneath it is
never renamed, ever** — only leaves move. A parent's own verdict is reported
(`ACTIVE (has sub-labels)`, `STALE (has sub-labels)`, etc.) purely for
visibility, never as something to act on.

## Every verdict, in the order they're decided

1. **`ARCHIVE CONTAINER`** — the label itself *is* an `Old`/`zOld` folder.
   Structure, not content — never moved, never deleted, checked first because
   nothing else about it matters once this is true.
2. **`... (has sub-labels)`** (`ACTIVE`/`STALE`/`ARCHIVED`/`EMPTY`, each with
   this suffix) — has children, reported but never renamed. See above.
3. **Archived leaves** — any leaf with `Old` or `zOld` as a path segment:
   - `EMPTY (archived)` — no mail at all.
   - `ARCHIVED` — mail exists, but not within the revival window (see below).
     Correctly filed, nothing to do.
   - `REVIVE` — mail has arrived within `REVIVE_MONTHS` of today. Proposed
     to move back up a level, by dropping the archive segment nearest the
     leaf (`Dosh/Banks/Old/Acme Bank` → `Dosh/Banks/Acme Bank`).
4. **Unarchived leaves**:
   - `EMPTY` — no emails ever. Reported for manual deletion; the scripts
     never delete anything.
   - `ACTIVE` — mail within `STALE_YEARS`.
   - `STALE` — no mail for `STALE_YEARS`. Proposed to move under `Old`
     (reusing an existing `Old`/`zOld` at the right level if one exists,
     never creating a duplicate).

## Archiving and reviving use different thresholds, on purpose

A label archives after 2 years of silence, but only revives if mail has
arrived within the last 6 months — a much tighter, more recent bar than the
one that archived it in the first place. A label dormant for 18 months that
gets one old email isn't offered for revival just because it isn't yet
2-years stale; reviving is meant to catch mail that's *actually* started
again, not merely mail that technically isn't ancient yet.

Revival is also a **hard cutoff, with no borderline grace zone** — unlike the
stale/active decision below, there's no judgement-call band around the
6-month line. Archiving does have one: anything within `BORDERLINE_DAYS`
either side of the 2-year line is flagged `BORDERLINE` in the `NOTE` column
and defaults to `SKIP`, on both sides — it already carries a proposed name,
so flipping `ACTION` to `MOVE` is all that's needed to include it.

Revival can only be triggered by genuinely new mail — ageing alone only ever
pushes a label further past whichever line it's being measured against, so a
label can never oscillate between `STALE` and `REVIVE` from one run to the
next without a real new email landing on it.

## Collisions

If the proposed new name already exists as a label, the row is left as
`SKIP` with a note explaining why, regardless of which path (archive or
revive) proposed it. Nothing is ever renamed onto an existing label.

## The apply step

`apply_moves.py` reads back the `ACTION` column and processes every `MOVE`
row **deepest-path-first** — so a parent's own move (if it somehow qualified)
would never happen out from under a child mid-run. It's a dry run by default;
`--apply` is required to actually call the Gmail API. See
[Security and Privacy](Security-and-Privacy) for why the apply step can't
read your mail even if it wanted to.
