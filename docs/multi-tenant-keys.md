# Phase 2 — the multi-tenant key layout

**Status:** design, agreed 2026-09-01. Implementation follows this document.

One table, Query-only on the partition key, same discipline as GreyScale. Three
partition namespaces, and which one a row lives in is decided by a single
question: **is this a fact about the WoW guild, or a record of what this install
did?**

```
WOW#<region>#<realm>#<name>   facts from upstream. SHARED by every tenant.
TENANT#<discord_guild_id>     what this install has done. NEVER shared.
ART#GLOBAL                    boss artwork. Global, unchanged.
```

## Why split rather than key everything by tenant

Two reasons, and the second is a correctness bug rather than an optimisation.

**The WCL budget is the scaling constraint.** The API budget (~3,600 points/hour)
is shared across all tenants. Keying progression, rosters and kill records by
tenant means N guilds tracking the same WoW guild costs N times the points for
identical data. Keying them by WoW guild means it costs once. That is the
project's distinctive engineering story and it lives or dies on this key.

**Dedupe must NOT be shared.** Today the `announced` string set lives on the
`TIER#<slug>` row under the WoW guild. If two Discord servers ever track the same
WoW guild — a guild's main server and its social server, entirely reasonable —
they would share that set, and whichever polled second would find every boss
already marked announced and silently post nothing. Not an error, not a log line:
a bot that looks fine and never speaks.

So `announced` moves to the tenant. The conditional write that makes posting
exactly-once keeps working exactly as it does now; it just locks per install
instead of per guild.

## Row placement

Derived by asking whether a second Discord server tracking the same WoW guild
should see the same value.

### `WOW#<region>#<realm>#<name>` — shared

| sk | what | why shared |
|---|---|---|
| `PROGRESS` | progression snapshot | a fact about the guild, identical for everyone |
| `TIER#<slug>` | tier baseline, kill counts, raid name | ditto |
| `KILL#<slug>#<boss>` | first-kill roster and timestamp | ditto |
| `ROSTER#<slug>` | derived team classification | ditto |
| `SOURCE` | log-source-dark detection | a fact about the guild's logs |

### `TENANT#<discord_guild_id>` — never shared

| sk | what | why per-tenant |
|---|---|---|
| `CONFIG` | WoW guild, channel, role to mention | the install *is* this row |
| `ANNOUNCED#<slug>` | **the dedupe set** | two installs post to two channels |
| `AOTC` | AOTC announcement claim | same reason |
| `RECAP#<night>` | recap claim | same reason |
| `BOOTSTRAP` | has this install been seeded | a NEW tenant must not announce history it never saw |
| `HEALTH` | this install's health | one tenant's bad channel is not another's problem |

`BOOTSTRAP` is the subtle one. It reads like a fact about the guild, but its job
is "first run announces nothing". A second tenant joining a guild that is already
tracked has itself never announced anything, so it must bootstrap independently
or its first poll would replay the whole tier into a new channel.

## Key derivation is not negotiable

The Discord guild id comes from **the verified interaction payload**, never from
a field in a request body. This is the GreyScale rule — "no endpoint accepts a
member id" — transplanted. There is no code path where a caller supplies the
tenant they want to act as, so there is nothing to tamper with.

`TENANT#` rows are only ever reached by a pk built from that claim. A tenant
cannot read or write another tenant's rows because it cannot name them.

## What this costs in migration

`scrambled` becomes tenant #1 with no special-casing. Its 11 live rows split:
the `GUILD#` partition becomes `WOW#`, and the announcement state lifts out into
a `TENANT#<scrambled discord guild id>` partition.

**Proven on dev first.** The dev stage has its own throwaway table, so the new
layout gets exercised end to end before it touches the 11 rows that stop a boss
kill being announced twice in a live channel.

## Raid teams (added 2026-09-04)

A raid TEAM inside a guild is a third kind of install, and it breaks the "shared
upstream" rule on purpose.

```
TENANT#<discord_guild_id>#<team-slug>          what this team's install did
WOW#<region>#<realm>#<name>#<team-slug>        the team's facts, shared with nobody
```

Why the facts partition is NOT shared with the guild: the team's first kills are
not the guild's first kills, and the team's first-kill rosters (`KILL#`) must not
be read by the guild install's recap when it derives the prog roster. Sharing
`WOW#` would do exactly that. The WCL budget argument for sharing does not apply
either -- the team's source is a different query (`reports(userID:)`), so there is
no duplicate fetch to save.

Under `TENANT#…#<team>`, the announced set is per difficulty:

| sk | what |
|---|---|
| `ANNOUNCED#<slug>` | Heroic dedupe set + AOTC flag (the historical row shape) |
| `ANNOUNCED#<slug>#normal` | Normal dedupe set + "Normal cleared" flag |

Both rows also carry their own `baseline`, because a Normal seed of 3 is not a
Heroic baseline of 3. `store.load_tier` prefers the row's baseline and falls back
to the shared `TIER#` row's, so the guild's pre-existing rows read as before.

The team's `CONFIG` row adds `teamSlug`, `teamName`, `wclUserId`, `raidDays` and
`difficulties`. It is written by `scripts/register-team.py`, never by `/setup`.
The slug is validated to `[a-z0-9-]` so it can no more forge a `#` boundary than a
tenant id can.

## What Phase 2 does NOT do

No upstream caching layer yet — that is Phase 3, and it is what makes the shared
`WOW#` partition pay off. Phase 2 only has to put the data where Phase 3 can use
it. `/setup` lands minimal: pick and validate the guild, choose the channel. The
preview card follows later.
