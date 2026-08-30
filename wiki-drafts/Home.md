# Gmail label tidy-up

A local Python toolkit that tidies up a large Gmail label tree: dormant
labels move under category-level `Old` sub-labels, and archived labels that
start receiving mail again move back up. Nothing is ever deleted — renaming
a Gmail label preserves every email on it, and every move is reversible by
renaming back.

## At a glance

| | |
|---|---|
| **Runs on** | Your own machine, as a script — no server, nothing installed on Google's side beyond an OAuth client |
| **Reads mail** | Only `audit.py`, on a `gmail.readonly` scope that cannot change anything |
| **Changes labels** | Only `apply_moves.py`, on a separate `gmail.labels` scope that cannot read a single email |
| **Network exposure** | None — no port, no server, nothing reachable from outside your machine |
| **Data leaves your machine** | Only to Google's own Gmail API, over the same OAuth connection Gmail's own apps use |
| **Deletes anything** | Never — labels are renamed, not removed, and every rename is reversible |

## Who this is for

You have a Gmail account with a large, deep label tree that's become hard to
navigate, you're comfortable running a Python script from a terminal and
completing a one-time OAuth consent flow, and you want dormant labels filed
away automatically with a chance to review every proposed change before it
happens. If you want a GUI, multi-account support, or a tool that manages
labels on someone else's behalf, this isn't it — it's a personal, single-
account script.

## Where to go next

| I want to... | Go to |
|---|---|
| Install it | The [README](https://github.com/rohitafish/gmail-labels#one-time-setup) and [SETUP-LOCAL.md](https://github.com/rohitafish/gmail-labels/blob/main/SETUP-LOCAL.md) — OAuth client setup and the full run-through |
| Understand exactly how a verdict is decided | [How the Audit Works](How-the-Audit-Works) |
| See what each tunable constant does | [Configuration Reference](Configuration-Reference) |
| Know what this does with my data before I trust it on my account | [Security and Privacy](Security-and-Privacy) |
| Fix something that's not working | [Troubleshooting / FAQ](Troubleshooting-FAQ) |
| Contribute | [CONTRIBUTING.md](https://github.com/rohitafish/gmail-labels/blob/main/CONTRIBUTING.md) |

## Project links

- [Source & README](https://github.com/rohitafish/gmail-labels)
- [Report a security issue](https://github.com/rohitafish/gmail-labels/security/policy) — please don't open a public issue for one
- [Licence (MIT)](https://github.com/rohitafish/gmail-labels/blob/main/LICENSE)
- [Open issues](https://github.com/rohitafish/gmail-labels/issues)
