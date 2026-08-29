# Scrambled Raid Bot

Announces in Discord when the WoW guild **Scrambled** kills a Heroic raid boss for the
first time, and posts a separate AOTC card tagging Prog Raiders when the tier is cleared.

EventBridge Scheduler → Lambda → Discord webhook. No gateway connection, no server,
about **$0.02/month**.

---

## The rule the whole thing is built around

**A boss is announced on its first Heroic kill and never again.**

That is the only hard requirement, and it is harder than it sounds, because the bot polls.
Every fifteen minutes it sees the same raid night it saw fifteen minutes ago. A cold start
sees it again from scratch. The final boss dies again every week for the rest of the tier.
A duplicate post in `#bots` cannot be taken back and other people read it.

So the write that records a boss as announced **is** the permission to announce it:

```python
UpdateExpression:    ADD announced :boss
ConditionExpression: attribute_exists(pk) AND
                     (attribute_not_exists(announced) OR NOT contains(announced, :name))
```

A conditional update, not a read-then-write. If two invocations overlap — a slow poll still
running when the schedule fires, a retry after a timeout that did not actually fail —
exactly one wins the condition and posts. Read-then-write lets both read an empty set and
both post.

The claim is taken **before** the webhook call, and released again if the webhook
permanently fails. That is a deliberate trade in one direction: a missed announcement is
recoverable on the next poll, a duplicate one is not recoverable at all.

`scripts/selftest.py` runs first in `scripts/deploy.sh`, so a regression in any of this
blocks the deploy. It exercises the real `ConditionExpression`s against an in-memory
DynamoDB double, and ends with a full run of the handler proving the two things worth
proving: **a mid-tier deploy announces nothing**, and **a re-kill announces nothing**.

---

## Why not the #logs channel

Scraping posted Warcraft Logs links out of `#logs` only catches the kills somebody
remembered to paste, breaks whenever the link format shifts, and — the part that actually
matters — cannot tell a first kill from the ninth re-kill of the same boss, which is the
single distinction this bot exists to make.

---

## Architecture

```
EventBridge Scheduler ──rate(15 min)──▶ Lambda ──▶ Discord webhook  (#bots)
                                          │
                                          ├─▶ Warcraft Logs v2   what died, and when
                                          ├─▶ Raider.IO          how many, what rank
                                          └─▶ DynamoDB           has it been announced
```

The bot only ever announces. It takes no commands and handles no interactions, so there is
nothing for a persistent gateway connection to do except cost money to stay open and turn a
scheduled function into a process that has to be kept alive.

### Two sources, neither sufficient alone

**Warcraft Logs v2** is the event source. OAuth2 client credentials against
`/oauth/token`, then GraphQL at `/api/v2/client`: the guild's reports, each report's
fights, filtered to `killType: Kills` at `difficulty: 4`.

Its rate limit is **points per hour, not requests per hour**, and a query's cost depends
on what it asks for rather than how often. Picking a "safe" poll interval is therefore
tuning the wrong variable. Every query here asks for `rateLimitData` alongside the real
payload — it rides along free — and the run backs off on the fraction of the hourly
allowance already spent, stopping *before* the expensive reports query rather than after.

A fight's `startTime` is a millisecond **offset from its report's** `startTime`, not an
absolute timestamp. Treating it as absolute puts every kill in January 1970, which is only
obvious once an AOTC card is dated 56 years ago.

**Raider.IO** is the enrichment source. No API key:

```
GET /api/v1/guilds/profile?region=&realm=&name=Scrambled
    &fields=raid_progression,raid_rankings
```

`heroic_bosses_killed / total_bosses` gives "n of X"; `raid_rankings[raid].heroic.realm`
gives the server rank.

---

## Resolving the raid, which is the part that looks easy

The obvious approach is to slugify the Warcraft Logs zone name and look it up in
`raid_progression`. Here is what is actually in there, live on 2026-08-28:

| slug | name | bosses |
|---|---|---|
| `tier-mn-1` | MN Tier 1 (VS / DR / MQD) | 9 |
| `sporefall` | Sporefall | 1 |
| `the-tidebound-grotto` | The Tidebound Grotto | 1 |
| `the-venomous-abyss` | The Venomous Abyss | 8 |

No slugification of any zone name Warcraft Logs will ever report produces `tier-mn-1`. So
that approach does not fail loudly — it attributes those kills to a *different* raid and
corrupts the count.

Raider.IO publishes the mapping directly at `/api/v1/raiding/static-data`: every raid, its
slug, and its **ordered list of encounters by name**. So the raid is resolved from the boss
that actually died, which is the one fact the event source is certain about. Four rungs,
and the rung that fired is logged on every announcement:

1. **encounter name** matches a boss in a known raid ← the reliable one
2. the zone name slugifies to a key present in `raid_progression`
3. exactly one raid's published live window contains the kill timestamp
4. the last key in `raid_progression` (Raider.IO appends the newest tier last)

Boss names are compared normalised, because the two APIs agree on names but not on their
punctuation — `Vaelgor & Ezzorak` against `Vaelgor and Ezzorak`, and straight against
typographic apostrophes in `Belo'ren`. Comparing raw strings silently drops those to a
weaker rung.

Nothing is hardcoded to a tier. `build_index` widens its expansion search until it covers
the raids the guild's own profile reports, so the day `raid_progression` names a raid the
hinted expansion has never heard of, the search walks forward and finds it.

---

## State

One table, composite key, on-demand. The execution role grants no `Scan` and no
`DeleteItem`; removing a set member is an `UpdateItem ... DELETE`, so the rollback path
stays inside that grant.

| item | pk | sk |
|---|---|---|
| tier state | `GUILD#<region>#<realm>#<name>` | `TIER#<raid-slug>` |

Held per tier: the set of announced bosses, the seed size, the count baseline, and the
AOTC flag. **Tier rollover needs no migration and no detection** — a new raid slug is a new
sort key, so the announced set starts empty on its own.

---

## Edge cases, handled explicitly

**Raider.IO lags Warcraft Logs, sometimes by hours.** Ryan's call is to announce
immediately and accept a stale *rank* rather than sit on the news. The *count* gets no such
licence: "they are now 5 of 8" underneath "just killed the 6th boss" is visibly wrong. So
the count is `max(what Raider.IO says, what the bot knows from its own claims)`, and the
bot's own figure is its seed baseline plus every boss claimed since.

**First run must not announce the back catalogue.** The first time a tier is seen, the bot
reaches back across the whole tier and records what is already dead without announcing any
of it — otherwise its debut in `#bots` is eight cards for bosses that died in July. The
distinction from a genuine tier rollover is whether any of that history is *older than the
current poll window*: a rollover has only fresh kills, and those are announced. `SEED_ONLY=1`
forces silent seeding regardless.

**A tier already cleared before the bot existed.** Seeding sets the AOTC flag when
Raider.IO already reports `heroic_bosses_killed == total_bosses`, so no achievement earned
before the bot was watching gets celebrated retroactively.

**The final boss dies every week after AOTC.** Every one of those re-kills satisfies
"kills == total". The persisted flag is claimed conditionally, so only the first wins.

**Unranked guilds.** Raider.IO writes `0` for "not ranked yet". A guild that has not placed
is not the zeroth best guild on its realm, so the rank line is omitted rather than rendered
as `Ranked server #0`.

**Private guild logs.** A client-credentials token can only read public reports. If
Raider.IO shows Heroic kills while Warcraft Logs returns no reports at all, the bot logs
`no_reports_visible` naming that cause specifically — the design needs revisiting at that
point, and no amount of retrying will help.

---

## The role mention

A webhook message renders `<@&123>` as a role pill whether or not the ping fires, so a
mention that silently does nothing *looks* correct in the channel. It fires only when the
role is also listed in `allowed_mentions.roles`:

```json
{"content": "<@&PROG_RAIDER_ROLE_ID>",
 "allowed_mentions": {"parse": [], "roles": ["PROG_RAIDER_ROLE_ID"]}}
```

`parse: []` alongside it is what stops everything else — with a non-empty parse list, an
`@everyone` that ended up in a boss name would go out to the whole server under the guild's
own webhook.

---

## Cost

| | basis | $/mo |
|---|---|---|
| Lambda | 2,880 invocations, 256 MB, ~2 s vs 400,000 GB-s free | 0.00 |
| EventBridge Scheduler | 2,880 invocations | ~0.00 |
| DynamoDB on-demand | a few thousand tiny reads/writes | ~0.01 |
| SSM Parameter Store | 3 standard parameters | 0.00 |
| CloudWatch Logs | structured JSON, one line per poll | ~0.01 |
| | | **≈ $0.02** |

Warcraft Logs and Raider.IO are free. Nothing is provisioned; no NAT, no VPC.

---

## Layout

```
src/handler.py       poll, resolve, announce — the orchestration
src/wcl.py           Warcraft Logs v2: OAuth, GraphQL, rate-limit accounting
src/raiderio.py      Raider.IO: profile, static raid data, slug resolution
src/store.py         DynamoDB: the announce-once claim
src/discord.py       webhook payloads and retries
scripts/selftest.py  the gate; no AWS, no boto3, no network
scripts/deploy.sh    package + ship the Lambda, then verify admin-owned wiring
infra/iam-setup.sh   one-time admin setup: table, role, secrets, schedule
```

No dependencies beyond the standard library and the boto3 already in the runtime, so the
package is a handful of `.py` files and the deploy is a zip.

## Running it

```sh
cp .env.example .env      # fill in; .env is gitignored
scripts/selftest.py       # no AWS needed
scripts/deploy.sh
```

`infra/iam-setup.sh` covers the one-time table, role, secrets and schedule. It is
deliberately separate: the deploy identity has no IAM write, no SSM write and no Scheduler
write, so those are created once by an admin in CloudShell and then left alone.
`deploy.sh` verifies they exist and reports drift rather than attempting updates it can
only ever be denied.

Order matters once: deploy the function before creating the schedule, because EventBridge
Scheduler validates its target at creation time.

The three secrets live in SSM as SecureString and are never written into the Lambda's
environment — a Discord webhook URL is a post-anything-to-`#bots` credential, not a config
value, and environment variables are readable by anything that can call
`GetFunctionConfiguration`.
