# Configuration Reference

Every tunable constant lives at the top of `gmail_common.py`, in one
`CONFIG` block. There's no `.env` file and no command-line flag for any of
these — they're deliberately plain module constants, edited directly in the
source. If this page and the code ever disagree, trust the code and treat
this page as due for an update.

| Constant | Default | What it does |
|---|---|---|
| `SCOPE_PREFIXES` | `['Dosh', 'Politics', 'Business', 'Computing', 'Journeys', 'Friends']` | Top-level label prefixes the audit examines. Plain "starts with" match, so emoji and their invisible variation selectors never have to be typed. Set to `[]` to process every top-level label. |
| `STALE_YEARS` | `2` | A label is stale if it has no email newer than this many years. Governs the `STALE`/`ACTIVE` decision and the archiving side of the tree — see [How the Audit Works](How-the-Audit-Works). |
| `BORDERLINE_DAYS` | `183` | Rows within this many days either side of the `STALE_YEARS` line are flagged `BORDERLINE` and default to `SKIP` — a judgement call, not an automation call. Applies only to the stale/active decision, not to revival. |
| `REVIVE_MONTHS` | `6` | An archived label is only proposed for revival if it has received mail within this many months — deliberately a much tighter, more recent bar than `STALE_YEARS`. Hard cutoff, no borderline zone. |
| `OLD_NAME` | `'Old'` | Name used when a new archive sub-label has to be created. An existing `Old` or `zOld` at the right level is always reused first. |
| `ARCHIVE_SEGMENTS` | `('old', 'zold')` | Path segments (case-insensitive, exact match) that mark a label as already archived. A label with any of these as a segment is left entirely alone by the archiving side, and only considered for revival. |
| `CACHE_MAX_AGE_DAYS` | `7` | Cached last-email dates in `.audit_cache.json` older than this are discarded and every label re-read, so a run months later can never judge staleness on dates it never re-read. |

## Two gotchas worth knowing before changing anything

**`SCOPE_PREFIXES = []` and a top-level `zOld` folder.** Setting the scope to
everything doesn't automatically make a bare top-level `zOld` folder count as
an archive container if its name carries an emoji — `is_archived`/
`is_archive_container` match the segment `zold` *exactly* after
case-folding, and `zOld 💾` (with the emoji) doesn't equal `zold`. Add the
exact segment text to `ARCHIVE_SEGMENTS` first if this applies to you.

**`BORDERLINE_DAYS` is not reusable for `REVIVE_MONTHS`.** They intentionally
use different mechanisms (a symmetric grace band vs. a hard cutoff) — see
[How the Audit Works](How-the-Audit-Works) for why widening the revival
window with a borderline band the same size as the stale/active one would
make almost the entire window "borderline" and defeat the point of a tight
cutoff.
