# greyBot — Scrambled raid progress

Announces in Discord when the WoW guild **Scrambled** (Proudmoore-US) kills a Heroic raid
boss for the first time, and posts a separate AOTC card tagging Prog Raiders when the tier
is cleared.

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

## The first run announces nothing

Scrambled does not arrive as a blank slate. Its live Raider.IO profile is three cleared
tiers and a fourth in progress:

| slug | progress | |
|---|---|---|
| `tier-mn-1` | 9/9 H | AOTC earned months ago |
| `sporefall` | 1/1 H | cleared |
| `the-tidebound-grotto` | 1/1 H | cleared |
| `the-venomous-abyss` | 2/8 H | current |

A bot that starts polling against that and applies its ordinary rules posts a dozen
retroactive kill cards and a **false AOTC for a tier finished months ago**, in a live guild
channel, before anyone can stop it. So the first run is a separate code path rather than an
emergent consequence of the dedupe rules happening to agree:

```python
if not store.is_bootstrapped(pk):
    bootstrap(...)      # seeds every tier, never calls discord.post
    return              # handler exits here
```

`bootstrap()` writes one item per raid in `raid_progression` — the killed set, the count
baseline, and the AOTC flag pre-set on every tier already cleared — then records a
`BOOTSTRAP` marker so it happens once. It contains no call to `discord.post` and `handler()`
returns immediately after it, so "posts nothing" is structural, not a rule that could be
outvoted. It logs what it did rather than staying quiet:

```json
{"event":"bootstrap_complete","tiers":4,"announced":0,
 "note":"SEEDED, did not announce — no messages were posted on this run"}
```

**Seeding cannot depend on Warcraft Logs history**, which is the subtlety. The tier that
most needs seeding is `tier-mn-1`, cleared long enough ago that the log lookback may not
reach it at all — and a guild with private logs has no history at any depth. Seeded empty,
the next transmog run through it announces nine "first kills" from months ago. So
Raider.IO's count fills the gap against its published encounter order:

- **fully cleared** → seed every boss. No guessing: `killed == total` says they are all
  dead, whatever order they died in. This covers all three of Scrambled's finished tiers
  exactly.
- **partly cleared** → seed the first *N* in published order, unioned with whatever history
  did show, and log `seed_assumption` so the assumption is on the record rather than silent.

This is also why the dedupe key is the **normalised boss name** and not the Warcraft Logs
encounter id. The encounter id is the more stable identifier, but it exists only in
Warcraft Logs — and the case where seeding matters most is exactly the case where Warcraft
Logs has nothing to say. The boss name is the only identifier both APIs share.

A tier appearing *later* is a rollover, and takes the opposite approach: Raider.IO's count
must **not** seed it, because at rollover that count is describing the very kills about to
be announced. It seeds only from history older than the poll window, so a new tier's first
kill is announced rather than swallowed.

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
                                          ├─▶ SSM Parameter Store  who and what secrets
                                          ├─▶ Warcraft Logs v2     what died, and when
                                          ├─▶ Raider.IO            how many, what rank
                                          └─▶ DynamoDB             already announced?
```

### Configuration

Seven parameters under `/greybot`, read at runtime, cached per container, fetched in one
`GetParameters` call:

| parameter | type | |
|---|---|---|
| `/greybot/wcl/client_id` | String | Warcraft Logs OAuth client |
| `/greybot/wcl/client_secret` | SecureString | |
| `/greybot/discord/webhook_url` | SecureString | post-anything-to-`#bots` capability |
| `/greybot/discord/prog_role_id` | String | the role AOTC pings |
| `/greybot/guild/name` | String | `Scrambled` |
| `/greybot/guild/realm` | String | realm **slug**, lowercase-hyphenated |
| `/greybot/guild/region` | String | `us` |

None of this is a Lambda environment variable. The two secrets are credentials, and
environment variables are readable by anything that can call `GetFunctionConfiguration`;
the guild identity sits with them so there is exactly one place to look when the realm slug
is wrong rather than two that can disagree. The execution role names all seven ARNs
individually — a `/greybot/*` wildcard would also grant whatever gets added under that
prefix later.

`config.load()` fails loudly and names the missing parameters, because SSM answers a
request for a parameter that does not exist with a **200** and a quiet omission. The realm
is lowercased and hyphenated on the way in, since a display name (`Aerie Peak`) 404s the
Raider.IO call without saying why.

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

Note there is no "current tier" variable anywhere. The tempting shortcut — *the current
tier is the one that is neither fully cleared nor untouched* — works on today's data and
stops working at the worst possible moment:

| | today | the night they clear it |
|---|---|---|
| `tier-mn-1` | 9/9, cleared | 9/9, cleared |
| `the-venomous-abyss` | **2/8 ← current** | 8/8, cleared |
| tiers matching "neither" | 1 | **0** |

The heuristic returns nothing on AOTC night, which is the single most important
announcement the bot makes. It also returns nothing for a brand-new tier at 0/8. Resolving
per kill has neither failure mode, and handles the raid night that clears the new tier and
then farms an old one for mounts — one report, two raids, which no single current-tier
answer gets right.

---

## State

One table, composite key, on-demand. The execution role grants no `Scan` and no
`DeleteItem`; removing a set member is an `UpdateItem ... DELETE`, so the rollback path
stays inside that grant.

| item | pk | sk |
|---|---|---|
| bootstrap marker | `GUILD#<region>#<realm>#<name>` | `BOOTSTRAP` |
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

**First run must not announce the back catalogue.** See *The first run announces nothing*
above — it is the largest single risk in the project and has its own code path and its own
tests.

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

## The greyBot identity

The avatar people actually see beside each announcement is the **webhook's**, not the bot
user's. This bot has no gateway connection, so nothing ever logs in as a bot user — every
announcement is an HTTP POST, and Discord renders those under the webhook's own name and
avatar. Setting the application icon in the Developer Portal changes the icon on the *app*;
it does not change the face in `#bots`.

So there are two places, and they are set two different ways:

| what | where | how |
|---|---|---|
| application / bot user icon | Developer Portal → the app → Bot → Icon | by hand — there is no API for it with a bot token |
| **the face beside each announcement** | the `#bots` webhook | `scripts/set-webhook-identity.py` |

```sh
scripts/set-webhook-identity.py --check    # report current identity, change nothing
scripts/set-webhook-identity.py            # apply the name and avatar
```

It reads the webhook URL from `--webhook-url`, then `$DISCORD_WEBHOOK_URL`, then SSM. It is
idempotent, and it never prints the URL — `PATCH /webhooks/{id}/{token}` takes no
`Authorization` header, so the token in the URL *is* the credential.

Set once on the webhook rather than per message. The alternative — `username` and
`avatar_url` in every POST — requires the PNG served from a publicly reachable URL, which
means hosting it somewhere; the obvious somewhere is `ryangrey.dev`, which is deliberately a
zero-external-request single-file site. Setting it once sidesteps that entirely and keeps
the announcement payloads clean.

`assets/greyBot-avatar.png` is the canonical asset, version controlled alongside the code:
1024×1024 RGBA, `#12151A` field, `#8DBCEB` highlight outline with glow, `#6AA8E0` accent.
Discord never renders a webhook avatar above 128px and has historically rejected oversized
data URIs, so the script downscales to 256px for the upload (328 KiB → 47 KiB) and leaves
the master untouched.

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
src/config.py        the seven SSM parameters
src/wcl.py           Warcraft Logs v2: OAuth, GraphQL, rate-limit accounting
src/raiderio.py      Raider.IO: profile, static raid data, slug resolution
src/store.py         DynamoDB: the announce-once claim
src/discord.py       webhook payloads and retries
assets/              greyBot-avatar.png — the canonical icon, 1024x1024
scripts/selftest.py  the gate; no AWS, no boto3, no network
scripts/deploy.sh    package + ship the Lambda, then verify admin-owned wiring
scripts/set-webhook-identity.py   name + avatar on the announcing webhook
infra/iam-setup.sh   one-time admin setup (1 of 2): table, execution + scheduler roles
infra/create-schedule.sh   admin setup (2 of 2): the 15-minute poll, created last
```

No dependencies beyond the standard library and the boto3 already in the runtime, so the
package is a handful of `.py` files and the deploy is a zip.

## Running it

```sh
cp .env.example .env      # deploy-time values only; .env is gitignored
scripts/selftest.py       # no AWS, no boto3, no network
scripts/deploy.sh
```

**On first deploy, invoke it once by hand before letting the schedule run**, and confirm
the log line says it seeded:

```sh
aws lambda invoke --function-name ryangrey-greybot --region us-east-1 /dev/stdout
aws logs tail /aws/lambda/ryangrey-greybot --region us-east-1 --since 5m
```

Expect `{"event":"bootstrap_complete", ... "announced":0}`. If that first run produces an
`announced_kill` or `announced_aotc`, something is wrong — stop the schedule.

The one-time admin setup is split in two, and the order matters:

| | where | what |
|---|---|---|
| 1 | CloudShell | `infra/iam-setup.sh` — table, execution role, scheduler role |
| 2 | local | `scripts/deploy.sh` — ship the function |
| 3 | local | invoke once by hand, confirm it **seeded** |
| 4 | local | `scripts/set-webhook-identity.py` — greyBot name and avatar |
| 5 | CloudShell | `infra/create-schedule.sh` — start the 15-minute poll |

The schedule is deliberately last. EventBridge Scheduler validates its target at creation
time so the function must already exist, and starting a fifteen-minute timer before run one
is verified means the clock is ticking while you are still checking whether it announced
anything it should not have.

The split between CloudShell and local is the same one as the study engine: the deploy
identity has no IAM write, no SSM write and no Scheduler write, so those resources are
created once by an admin and then left alone. `deploy.sh` verifies they exist and reports
drift rather than attempting updates it can only ever be denied.

The three secrets live in SSM as SecureString and are never written into the Lambda's
environment — a Discord webhook URL is a post-anything-to-`#bots` credential, not a config
value, and environment variables are readable by anything that can call
`GetFunctionConfiguration`.
