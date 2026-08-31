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
                                          ├─▶ DynamoDB             already announced?
                                          └─▶ SNS ─▶ SES ─▶ email  still allowed to post?
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

Eight more are **optional**, and default to something safe when missing. They are fetched
in a separate call, chunked in tens, because `GetParameters` denies the whole request if
the caller lacks permission on any single name in it — asking for optional names alongside
required ones means one un-granted parameter takes the announcer down. That is not
hypothetical; it is what happened when the Discord interaction parameters were added
before the role was widened.

| parameter | default | |
|---|---|---|
| `/greybot/recap/enabled` | **false** | the recap posts nothing until this is turned on |
| `/greybot/recap/show_worst_parse` | **false** | parse-shaming starts arguments; opt in, never out |
| `/greybot/recap/schedule` | — | the cron; `infra/create-recap-schedule.sh` reads it |
| `/greybot/team/roster_min_first_kill_pct` | 50 | share of first kills that puts a player on the roster |
| `/greybot/team/prog_overlap_high` | 70 | roster overlap at or above this is the prog team |
| `/greybot/team/prog_overlap_low` | 35 | roster overlap at or below this is the other team |
| `/greybot/team/prog_tag` | — | Warcraft Logs report tag, if the guild ever starts tagging |
| `/greybot/alerts/sns_topic_arn` | — | where health alerts are mailed; unset means no alerts |

`recap/enabled` defaulting to false is the important one: deploying this code changes
nothing until somebody decides otherwise. The recap posts a card naming individual raiders
into a live guild channel, and "it started posting because a parameter appeared" is not an
acceptable way for that to begin.

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
| first-kill roster | `GUILD#<region>#<realm>#<name>` | `KILL#<raid-slug>#<boss-key>` |
| derived roster | `GUILD#<region>#<realm>#<name>` | `ROSTER#<raid-slug>` |
| recap claims | `GUILD#<region>#<realm>#<name>` | `RECAPS` |
| Discord standing | `GUILD#<region>#<realm>#<name>` | `HEALTH` |

Held per tier: the set of announced bosses, the seed size, the count baseline, and the
AOTC flag. **Tier rollover needs no migration and no detection** — a new raid slug is a new
sort key, so the announced set starts empty on its own.

The role grants no `Query` either, which shaped the roster storage rather than merely
constraining it. Nothing can be a collection the code discovers by scanning a key range,
so the participant records are one item per kill, addressable by a key the code already
has: the tier's `announced` set *is* the list of first kills. Nine `GetItem`s once a week
is not worth a cleverer scheme, and one item per kill stays readable in the console — the
same reason the announced set holds boss names rather than encounter ids. A boss in
`announced` with no `KILL#` item was seeded, killed before the bot was watching, and is
treated as long dead.

Recap claims are a string set on one item rather than an item per night, because releasing
a claim after a failed webhook has to be an `UpdateItem ... DELETE` to stay inside the
grant — the same shape as the boss claim, for the same reason.

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

## The morning-after recap, and the two-teams problem

The morning after raid night a second card goes up in `#bots`: top three damage, most
deaths, best parse, pull count on the progression boss, and what died. One embed, no ping.
It runs on a second EventBridge schedule pointed at the **same** Lambda with
`{"mode": "recap"}` — one function, two schedules, not a parallel stack.

Scrambled runs two raid teams into one Warcraft Logs guild, an A team and a B team, and
only the A team gets recapped. Nothing in the API says which is which.

### Why the roster is derived and not written down

A hand-maintained roster goes stale the first time somebody transfers, and a stale roster
fails silently. So the roster is derived from something the bot already knows for certain:
who was standing there for each of the tier's first Heroic kills. Under the guild's own
premise — the B team does not kill a Heroic boss before the A team — every first kill is
the A team's, so its participants are the A team by construction.

Membership is by **frequency**, not presence. A player has to appear in at least 50% of
the tier's first kills, so a one-off fill-in never enters the roster no matter how well
they played. That threshold has an arithmetic consequence worth knowing: a player who
raided once clears 50% on any sample smaller than three kills, so a roster derived from
fewer than three is marked *provisional* and the previous tier's roster is preferred until
real data replaces it. At a tier rollover the old roster is carried forward as a seed, and
every verdict reached against a seed is logged as such.

### Two signals, and a refusal

`resolve_team()` returns `PROG`, `OTHER` or `UNKNOWN`.

**Signal A — roster overlap.** What fraction of this report's raiders are on the derived
roster? This needs a *margin*, not a majority: several people raid on both teams, so a
B-team report legitimately contains A-team players and a 51% rule would call it prog about
half the time. Above 70% is prog, at or below 35% is not, and the gap between them is the
bot admitting the number does not know. An empty roster yields `None`, never `0.0` — those
must not collapse, or "we have no roster yet" becomes a confident `OTHER`.

**Signal B — progression evidence.** Does the report contain a Heroic encounter that was
not dead when the report started? The B team farms what is already dead; pushing an undead
boss is the A team essentially by definition. This is what carries the cold start, where
the roster is empty or merely seeded.

Two details in signal B are deliberate departures from the obvious reading. It compares
against the bosses dead **before this report began**, not against everything the bot has
ever announced — the announcer polls every fifteen minutes, so a boss killed at 9pm is in
the announced set long before the recap runs, and comparing against that set would erase
the very evidence identifying the night. And it counts a first *kill* as progression, not
only wipes: a one-pull clear of the last undead boss is not less the A team for having
been efficient, and excluding it would make the bot abstain on the report the guild most
wants to see.

One conclusive signal decides. Two that disagree do not. **`UNKNOWN` posts nothing**, and
logs which signals were inconclusive. This is the same rule that removed the
raid-resolution fallback from the announcer, and it matters more here: a recap names
individual people and can publish their worst parse. Silence beats a confident wrong
answer.

The kill announcer is unchanged. It announces only the *first* Heroic kill of each boss,
so under the premise above every announcement is already the A team's and the existing
dedupe suppresses the B team's later kills of the same boss. Adding team filtering there
would be solving a problem that does not exist.

### Report tags: supported, unused

Warcraft Logs has exactly the right feature for this. Introspection confirmed
`reportData.reports(guildTagID:)`, `Report.guildTag`, `Guild.tags` and even `Guild.teams`
all exist. Scrambled uses none of them — `tags: []`, `teams: []`, and every recent report
comes back with `guildTag: null`.

So the derived roster does the work. But the tag path is built and checked first, so **if
the guild ever tags its two teams**, setting `/greybot/team/prog_tag` turns a statistical
guess into an authoritative answer with no code change. That is the single highest-value
thing anyone could do to make this more reliable.

### What the API actually returns

`table` and `rankings` return the untyped `JSON` scalar. Their contents are not in the
schema and are not documented, so the parsers were written against a real captured
response — `scripts/fixtures/report-recap.json`, trimmed, with character names substituted.
Six things in the real data would each have broken a parser written from a careful reading
of the docs. The last two were invisible in a single-report fixture and were caught by the
first live dry run, which is what that dry run is for:

**One report is not one activity.** The captured report holds sixteen Heroic raid fights,
a Normal kill, *and three Mythic+ dungeon runs*. A DamageDone table over the report's full
time range returned 27 entries for an 18-person raid, eight of them people who never
entered a Heroic fight — and a different top three, led by a player whose total was mostly
dungeon damage. The tables are scoped to explicit `fightIDs`. Still one table call per
report; the point budget is unaffected.

**One report is not one raid, either.** That same report opens with a Heroic kill in *The
Tidebound Grotto* and then spends the night in *The Venomous Abyss*. Taking the tier from
the first fight — the obvious reading — labels the night with a raid it visited for two
pulls. The tier is the one holding the most of the night's Heroic fights, and fights from
elsewhere are dropped from the card — *including* from the rankings, which is subtler than
it sounds. Filtering parses by difficulty alone left a card correctly labelled "The Venomous
Abyss" crediting its best parse to Nymrissa Wavecaller, a boss in a different raid that the
same card had already excluded from its own list of what died.

**The Deaths table stops at 200 rows and does not say so.** The captured night had 245
deaths; a single call returned the first 200, ending 37% of the way through, with nothing
in the payload indicating truncation. Most importantly it named the *wrong person* as most
deaths. Deaths are read with a timestamp cursor until a short page comes back.

**Two people in the guild both log.** A single Thursday produced *two* reports of the same
pulls — `Prog Raid` by one raider and `Starting Heroic - 8/27` by another, starting four
minutes apart, 98% overlapping, with all three Heroic kills present in both at identical
wall-clock times. That is not the same as a night logged in two *parts*, which also happens
when a log restarts at the break, and the two need opposite handling: parts must be summed
or an hour of the night vanishes, duplicates must not be or every number doubles. Time
overlap separates them. Where two reports overlap by more than half of the shorter, the
more complete one is kept.

**Actor ids are scoped to one report.** Warcraft Logs renumbers actors per report, so id 2
in one log is a different person from id 2 in another. Every aggregate keys on the player —
name and server folded together — and an actor id is only used to look that player up
inside the report it came from. Aggregating by id splits one raider in half and fuses two
strangers together.

**`masterData.actors` is not a roster.** It returned 2,295 players across 130 realms — it
is every actor the log ever saw. The raiders are the ids in each fight's `friendlyPlayers`;
actors is only how an id becomes a name and a server. Damage entries carry no server at
all, and one of them was a warlock's pet.

Nothing in `src/recap.py` raises on a missing field. A blob that has lost a key costs the
card that one section and nothing else — the alternative is a parser that takes the whole
recap down the week Warcraft Logs renames something.

### Who counts, explicitly

Pugs and trials appear in the tables and must not top a guild leaderboard. The rule:
eligibility is *took part in a Heroic raid fight tonight*, intersected with the prog roster
when there is a usable one. Without a usable roster the intersection is dropped rather than
guessed — excluding real raiders is worse than occasionally including a guest. Pets are
excluded by actor type, and ties are shown in full rather than resolved by alphabet, because
three people level on deaths is a normal outcome of a wipe night.

### The point budget

`table` and `rankings` are materially more expensive than the fight queries the announcer
uses. `RateLimitData` is checked **before** any expensive call — checking afterwards would
report the damage rather than prevent it — and the recap stands down if fewer than 750
points remain.

That number is larger than the headroom `POINTS_CEILING` already reserves, and it has to
be: at 0.85 of a 3,600-point allowance the announcer's own check stops everything with 540
points left, so any recap reserve below 540 could never bind and would be decoration. 750
makes the recap yield *while the announcer still has room*, which is the entire reason
there are two numbers. Recapping one night measures at roughly 25 points, so the reserve is
thirty times what the job needs — deliberately. A recap that skips a week is fine. A first
kill that goes unannounced is not.

### Seeing a card before switching it on

```sh
aws lambda invoke --function-name ryangrey-greybot --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"mode":"recap","dry":true,"hours":48}' /dev/stdout
```

`hours` widens the lookback and is accepted **only** on a dry run — nothing in a scheduled
event payload should be able to move the window, or a stray field could make the bot recap
a night it was never meant to see. It exists because the useful moment to look at a card is
rarely the morning after a raid; without it you would have to wait for the next Wednesday
to see anything at all.

Renders the card from last night's real data and returns it in the response. It posts
nothing, and it **writes nothing** — not the claim, not the derived roster, not a rollover
seed — so it is safe to run repeatedly against production. It deliberately ignores
`/greybot/recap/enabled`, because a preview gated behind the switch it exists to inform is
not a preview.

The no-claim part is not a nicety. A dry run that took the ordinary path would mark the
night posted, and the real recap would then be correctly, silently and permanently
skipped: the whole point of the feature, defeated by the demo of it. The kill preview has
the same property for the same reason.

### Known edge case: the first bosses of a new tier

At a fresh tier the killed-boss set is empty, so signal B reads *any* progression as the A
team. If the B team raids first on opening night, it can take genuine first kills on the
easy encounters, and greyBot will announce them as prog and may recap that night as the A
team. This is understood and accepted for now; revisit before the next tier. Tagging the
two teams in Warcraft Logs would close it outright.

### Schedule, and the DST trap

Scrambled raids Tuesday and Thursday, 9pm to midnight Eastern, so the recap fires Wednesday
and Friday at 10am Eastern — two cards a week, each covering one night.

```
cron(0 10 ? * WED,FRI *)   timezone America/New_York
```

**The timezone argument is the point.** A bare UTC cron for 10am Eastern is 14:00 in summer
and 15:00 in winter, so it drifts an hour every November and every March and has to be
edited by hand twice a year. `--schedule-expression-timezone` keeps 10am at 10am.

The kill poller has never had this problem and does not need the fix: it runs on
`rate(15 minutes)`, which has no relationship to wall-clock time at all.

A raid that runs past midnight is still one night. The exactly-once key is the local date
the report **started**, so a Tuesday raid ending at 12:40am is claimed as Tuesday and
cannot be re-posted as Wednesday.

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

`assets/greyBot-avatar-256.png` is that downscale, committed rather than generated on the
fly. The script downscales with `sips`, which exists on macOS and not in CloudShell — and
CloudShell is where this runs, because the deploy user cannot read the webhook from SSM.
Pass it with `--avatar` there. Regenerate it from the master with:

```sh
sips -Z 256 assets/greyBot-avatar.png --out assets/greyBot-avatar-256.png
```

---

## The /progress slash command

The announcer needs no gateway connection, and neither does a slash command: Discord POSTs
each interaction to an HTTPS endpoint, so this is API Gateway → the same Lambda.

```
/progress → Scrambled — 2 of 8 in Heroic The Venomous Abyss, ranked server #67
```

Ephemeral by default (`EPHEMERAL_REPLIES=0` to make replies public), and styled exactly
like a kill card so the two read as one product.

Three parts of Discord's contract are unforgiving, and each shapes the code:

**The signature covers the raw body bytes.** Parsing the JSON and re-serialising it changes
the byte sequence and every signature then fails in a way that looks exactly like a wrong
public key. API Gateway may also deliver the body base64-encoded, so `isBase64Encoded` is
honoured *before* verification. There is a test asserting that re-serialising would have
broken it, so nobody later "tidies" that into a parse-and-dump.

**Discord probes with deliberately invalid signatures.** An endpoint that ever answers 200
to one is removed, with an email and a system DM about it. The rejection path is therefore
tested as hard as the acceptance path — against real Ed25519, because a stub would happily
agree with an implementation that had the arguments the wrong way round. A missing public
key rejects too: an endpoint that cannot verify must never wave anything through.

**Three seconds, including cold start.** A Lambda cannot answer "deferred" and then keep
working — execution stops when the handler returns. So the fast path avoids deferring
entirely: the poller already calls Raider.IO every fifteen minutes and leaves a small
snapshot behind, and `/progress` answers from a single `GetItem`. When that snapshot is
missing or stale, the slow path responds deferred and asynchronously invokes a second copy
of the function to fetch live and PATCH the follow-up, which is why the role grants
`lambda:InvokeFunction` on itself.

This is also why the function no longer reserves a concurrency of 1. That was harmless
when it only polled; with a slash command sharing it, a command queued behind a running
poll would fail visibly in chat.

Commands are registered **per guild**, not globally — guild commands appear instantly,
global ones take up to an hour to propagate, and this bot lives in one server. `PUT`
replaces the whole set, so re-registering cannot accumulate duplicates.

```sh
aws lambda invoke --function-name ryangrey-greybot --region us-east-1 \
  --cli-binary-format raw-in-base64-out --payload '{"admin":"register_commands"}' /dev/stdout
```

The application id is read from `/users/@me` rather than configured — for a bot, the user
id *is* the application id, which removes a fourth parameter to keep in step.

### On the Active Developer Badge

This command was originally built to qualify for Discord's Active Developer Badge.
**Discord discontinued that badge on 5 December 2025**, removing it from every profile —
they judged it a support burden that benefited developers little. It cannot be earned, so
nothing here chases it.

The command stays because the second reason was the real one: the guild wants progress on
demand, not only at kill time. `/progress` answers in about a tenth of a second from state
the poller already maintains.

The verified checkmark is a separate thing and needs the bot in 100+ servers. This one is
single-guild by design and will never qualify; there is deliberately no code chasing that
either.

---

## Knowing when it has been thrown out

Nothing else in this bot would notice being kicked. The announcer posts through a
**webhook, and a webhook is not a member** — so a kick, a ban or a timeout leaves every
poll looking exactly like a quiet week. The first sign of trouble would be somebody in the
guild asking why the last three kills went unannounced.

So each poll asks Discord four questions before it does anything else, and mails
`rgrey.web@gmail.com` when the answer changes:

| probe | endpoint | catches |
|---|---|---|
| identity | `GET /users/@me` | a regenerated or revoked bot token |
| installation | `GET /applications/{a}/guilds/{g}/commands` | **removed from the server** |
| membership | `GET /users/@me/guilds` | whether a bot member exists at all |
| member | `GET /guilds/{g}/members/{u}` | timed out, server-muted, deafened |
| webhook | `GET {webhook_url}` | the thing announcements actually go through, deleted |

The webhook probe is not a formality — it is the one that catches announcements stopping
while every other probe still reads healthy. Removing an app from a server deletes the
webhooks that app created, and a channel can be deleted out from under a webhook that was
made by hand.

### Installation, not membership, is the authority

**This cost a false alarm on the first live run, and the mistake is worth recording.**
greyBot was authorised to Scrambled with the `applications.commands` scope and never with
`bot`. It therefore has no member in the guild and never has: `/users/@me/guilds` returns
`[]` and `/guilds/{id}` answers `404 Unknown Guild`. Nothing is wrong with that —
announcements go through a webhook a person created, slash commands arrive at the
interactions endpoint, and neither needs a member. The first version read that absence as
a kick and mailed *"greyBot is no longer in the Scrambled Discord"* about a bot that was
working perfectly.

So the question "have we been thrown out" is asked of the **guild commands** instead.
Those are stored against the app's authorisation in that guild, so the endpoint answers
200 exactly while the app is installed and `403 Missing Access` once it is not — whether
it was kicked, banned, or removed from the server's Integrations page. That holds for an
app with no bot member, which is what this one is. It is deliberately a `GET`:
registration uses `PUT` against the same path, and a health check must never be able to
change the command set it is checking.

Membership is now **reported, not judged**. It becomes an alert only as a regression — a
member that existed on the last check and does not now — and that comparison lives in
`handler.py`, because only the stored state knows what was true before. `health.py` cannot
see history, so it must not be the thing deciding.

**A kick and a ban look identical from in here**, and that gap is not worth closing:
reading the ban list requires being in the guild, which is exactly the thing that just
stopped being true. The email names the probe rather than guessing between the two.

Membership is asked of the guild *list*, not of the guild, since `GET /guilds/{id}`
answers a non-member with 403 or 404 depending on the reason and 404 also covers "that id
was never a server". The one hedge is page size — 200 is that endpoint's maximum, so a
full page means absence proves nothing and the verdict is *unknown*.

### One email per event

The rule is that a **definite** answer differing from the last definite answer is an event,
and nothing else is. Ninety-six polls a day against a bot kicked on Tuesday produce one
email on Tuesday, one reminder every 24 hours after that, and one more when it is fixed.

`definite` is the load-bearing word. A 429, a 502 or a dropped connection is a bad minute
at Discord, and it is **not written down at all** — because if an unreachable Discord were
recorded as a state, the next successful poll would read as a change and mail an all-clear
for an outage that never happened. Worse, an unreachable Discord *during a real kick* would
clear the alert. Only answers that can mean exactly one thing are allowed to send mail.

`communication_disabled_until` gets the same treatment from the other direction: it is a
timestamp Discord leaves behind after it expires, so a past date means the timeout **ended**.
Read as a boolean it would keep mailing about a punishment that was already over.

The check runs *before* any Warcraft Logs work. Every other branch below it can return
early — rate-limit backoff, an idle poll, a recap yielding its budget — and a check placed
after any of them would go dark exactly when the bot went quiet, which is the moment it
exists for. It is also wrapped: it is an observer, and nothing it can do, including SNS
being unreachable, may stop an announcement going out.

### Why SNS and not SES

greyBot does not talk to SES. It publishes to `ryangrey-dev-alerts`, the topic that already
fans out to `ryangrey-alert-forwarder`, which sends from `alerts@ryangrey.dev` over a
DKIM-signed domain identity. That pipeline exists because **SNS's own email sender never
delivered to the target Gmail address** — four subscription attempts, zero arrivals — while
mail from an authenticated domain gets through. The forwarder already falls through to a
plain message body for anything that is not a CloudWatch alarm, so greyBot publishing a
subject and a block of text needed no change on that side. A second SES sender here would
mean a second reputation, a second set of DNS records, and a second thing to debug the next
time mail goes missing.

An unset `/greybot/alerts/sns_topic_arn` is how the alerts are turned off: the probes still
run and still log, they simply have nowhere to mail. The state is recorded either way, so
wiring the topic up later still gets the outstanding alert out.

Proving the whole path without waiting for something to break:

```sh
aws lambda invoke --function-name ryangrey-greybot --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"admin":"health","notify":true}' /dev/stdout
```

---

## Cost

| | basis | $/mo |
|---|---|---|
| Lambda | 2,880 invocations, 256 MB, ~2 s vs 400,000 GB-s free | 0.00 |
| EventBridge Scheduler | 2,880 invocations | ~0.00 |
| DynamoDB on-demand | a few thousand tiny reads/writes | ~0.01 |
| SSM Parameter Store | 3 standard parameters | 0.00 |
| CloudWatch Logs | structured JSON, one line per poll | ~0.01 |
| SNS + SES | one email per event, not per poll | ~0.00 |
| | | **≈ $0.02** |

Warcraft Logs and Raider.IO are free. Nothing is provisioned; no NAT, no VPC.

---

## Layout

```
src/handler.py       poll, resolve, announce — the orchestration
src/config.py        the seven SSM parameters
src/wcl.py           Warcraft Logs v2: OAuth, GraphQL, rate-limit accounting
src/raiderio.py      Raider.IO: profile, static raid data, slug resolution
src/store.py         DynamoDB: the announce-once claim, the prog roster
src/team.py          which of the guild's two raid teams filed a report
src/recap.py         reading the untyped table/rankings blobs
src/discord.py       webhook payloads and retries
src/health.py        can the bot still speak in the server — kick, ban, timeout, webhook
src/notify.py        publish one alert to the ryangrey-dev-alerts topic
src/interactions.py  slash commands: Ed25519 verification, PING/PONG, /progress
assets/              greyBot-avatar.png — the canonical icon, 1024x1024
scripts/selftest.py  the gate; no AWS, no boto3, no network
scripts/fixtures/report-recap.json  a REAL Warcraft Logs response, trimmed
scripts/introspect-wcl.py  ask the API what its schema is, before writing a query
scripts/deploy.sh    package + ship the Lambda, then verify admin-owned wiring
scripts/set-webhook-identity.py   name + avatar on the announcing webhook
infra/iam-setup.sh   one-time admin setup (1 of 2): table, execution + scheduler roles
infra/create-schedule.sh   admin setup (2 of 2): the 15-minute poll, created last
infra/grant-recap-config.sh       widen the role for the recap, create its parameters
infra/create-recap-schedule.sh    the Wed/Fri morning recap schedule
infra/create-interactions-api.sh  the HTTPS endpoint Discord posts interactions to
infra/grant-interactions.sh       widen the role for slash commands
infra/grant-alerts.sh             health alerts: the parameter + sns:Publish on the role
infra/reset-state.sh              DESTRUCTIVE: clear one guild's state so the next run reseeds
docs/wcl-reportdata-blind.md      incident record: Warcraft Logs returned no reports for 18h
```

One dependency: **PyNaCl**, for Ed25519 signature verification. Signature checking is not
somewhere to hand-roll crypto, so the package vendors the audited binding to libsodium.
The wheel has to match the Lambda's architecture rather than the laptop's, so `deploy.sh`
downloads the `linux/aarch64` build directly — no Docker needed — and the function runs on
**arm64**. It is lazily imported, so the scheduled poller never pays for it on a cold start.

Everything else is the standard library plus the boto3 already in the runtime.

The self-test needs PyNaCl too, to exercise signature verification against real Ed25519.
`deploy.sh` creates a `.venv` for it; nothing is installed system-wide.

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
