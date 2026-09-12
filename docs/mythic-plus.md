# Weekly Mythic+ recap

This feature uses the existing raid Lambda, DynamoDB table, recap bucket/site and
Discord bot delivery, with the raid recap's shared card renderer, Inter fonts,
Primer palette and page stylesheet. No new host is required.

## Reporting rules

- Publish Tuesdays at 10am America/New_York, following daylight saving time.
- Number reports from the regional main Mythic+ season's launch week, not from
  greyBot's first post. Raider.IO's static season metadata is refreshed daily;
  special-event seasons are excluded. Midnight Season 2 started August 18, 2026
  in the US, so August 18–25 is Week #1 and September 8–15 is Week #4.
  Sources: [Raider.IO season metadata](https://raider.io/api/v1/mythic-plus/static-data?expansion_id=11)
  and [Blizzard's season announcement](https://news.blizzard.com/en-us/article/24294369/midnight-season-2-is-now-live).
  Cards, Discord titles and full recap headings share the same computed week label.
  Week numbering resets with the next main season; `MPLUS_EXPANSION_ID` defaults
  to 11 (Midnight) and must follow the expansion when that changes.
- Reporting window: the preceding Tuesday at 10am through this Tuesday at 10am,
  start inclusive/end exclusive. This is an explicit reporting week, not a claim
  about Blizzard's maintenance/reset time. Include the interval on every recap.
- Count observed completed runs with at least two guild characters. Timed boards
  require completion within the run's own time limit. Deduplicate by season/run ID.
- Require all five distinct roster slots to be present before classifying a run.
  Freeze guild membership when the run is first collected, with that limitation
  disclosed; do not infer historical membership from today's roster on each recap.
- Highest timed key: each qualifying guild character's highest timed key.
- Weekly IO gain: overall score change for characters with a qualifying guild run,
  including points earned in other groups, as approved by Ryan.
- Highest overall IO: every guild character with a fresh same-season score at the
  reporting cutoff, even without any guild-group runs that week.
- Timed +10 runs: qualifying timed completions at level 10 or higher per character.
- All-guild highest: each character's best timed key whose entire five-character
  party is guild; one row per character, with the dungeon and source run link.
- All-guild runs: each character's timed completions in full-guild groups.
- Alts remain separate characters; no unverified Discord/account-name matching.
- Positions are consecutive and unique; deterministic name ordering breaks display
  ties. Each character appears once per category. The Discord card shows up to three entries; each website leaderboard is
  capped at 20 entries, including ties (deterministic name ordering at the cutoff).

## IO calculation

For Midnight Season 2 Weeks 1–4, label the gain category **Archived IO gain**.
Weeks 1–3 use recovered addon observations where comparable; Week 1 lacks a launch
baseline and remains unavailable. Week 4 compares the September 8 integer addon
baseline with the fresh September 15 live cutoff score, explicitly marked
approximate. Store imported observations under `ARCHIVED_SCORE#2026-09-08`, never
in the live SCORE partition. Week 5 and later use **Weekly IO gain**, requiring
fresh live observations at both weekly boundaries; no archived fallback.
Recap HTML requires cache revalidation, and updated links carry a format version
so browsers do not reuse a previously cached obsolete page.

Overall weekly IO change and gains earned only in qualifying guild runs are not
the same number. The approved calculation compares overall score snapshots for
characters who participated in at least one qualifying run, including score they
earned with other groups; the card/page explain that scope. Ryan explicitly approved
including points earned with other groups. Never label an inferred subset of score
as the actual overall delta.

Missing weekly baseline, boundary sample older than an hour, or a season change
means unavailable, not zero. A first-week recap cannot fabricate the prior week's
baseline. Collection should run before enabling publication.

## Collection and operational controls

Raider.IO guild members, character recent/best/weekly-highest runs and run-details
provide rosters, timing and scores. Collect continuously: these API lists are not
an exhaustive historical ledger. WCL uploads are not required for these six
categories; combat-performance analysis can be a later feature. Do not claim all
runs were collected. Late API updates and source errors can undercount results.

One minute collector schedule invokes `{"mode":"mplus_collect"}`; a 35-second
budget and a persistent cursor work through the roster, with a DynamoDB lease
preventing overlapping collectors. Existing records remain unchanged on failed
requests. Errors are recorded as coverage issues without raw response payloads.

Weekly schedule: `cron(0 10 ? * TUE *)`, `America/New_York`, flexible window OFF,
input `{"mode":"mplus_recap"}`. This is infrastructure scheduling, not a chat reminder.

Runtime environment: `MPLUS_ENABLED=1`, `MPLUS_CHANNEL_ID` set to the configured
channel ID. `MPLUS_SCORE_POLICY=overall_for_participants` is required for the approved
overall-score policy to publish. Preserve all
existing Lambda environment variables and other schedules during rollout.

Dry recap: `{"mode":"mplus_recap","dry":true}` returns calculated standings without
posting or uploading. A persistent weekly publication claim prevents duplicates.
Any publication failure becomes `needs_review`, including ambiguous Discord
timeouts; inspect the existing message before retrying rather than automatically
releasing its claim. Source state stays in DynamoDB, not the public website.
Publication stops if collection has not succeeded within the previous hour.
Score snapshots reflect the API's latest observed value, not a guarantee that
Raider.IO refreshed the character immediately before the reporting boundary.

## Rollout

Run `scripts/test_mplus.py` and the existing `scripts/selftest.py` before building
with `scripts/build-lambda.sh`. Back up the existing Lambda package and configuration,
compare its source with the repository baseline, and run publication guards against
the unpacked package. Update function code with a revision precondition and verify
its SHA-256; do not use an environment replacement that drops existing variables.

`infra/configure-mplus.py` previews by default. Supply the AWS profile, existing
function name, existing recap schedule name, and destination channel ID. `--apply`
adds only an IAM Query grant restricted to Mythic+ partitions, merges the new
environment variables, enables collection and leaves weekly publication disabled.
After successful live collection and a dry recap, add `--publish --apply` to enable
Tuesday publication. Existing raid schedules are not modified. These additive
schedules and the Query policy are managed by this script, separately from the
original CDK baseline; rerun it after a full CDK rollout to reconcile configuration.

Verify fresh collector state, score snapshots, retained run records, the configured
channel, Lambda result and both schedule states. Do not send synthetic standings to
the guild during validation. The first Tuesday may have partial run coverage and no
IO ranking until two real weekly boundary snapshots exist.

## New dungeon records

The record dispatcher checks every minute and posts to the same configured channel.
Enable its schedule with `infra/configure-mplus.py --records --publish --apply`
and the existing required arguments. It first waits for one complete roster pass
under the record collector, then quietly seeds records from retained runs including
available season-best runs. This avoids announcing the historical backlog.

Each season/dungeon has a highest timed level and the fastest completion at that
level. A higher timed level or a strictly faster equal-level run sets a new record;
an equal time, lower level, untimed run or group with fewer than two guild characters
does not. Alerts name the guild characters, dungeon, key level, elapsed time to
milliseconds, time limit and previous record, with a link to the source run.

These are observed guild records, subject to the same source-history limitations
as the weekly boards. Late discovery of a pre-activation run can improve the baseline
silently. Run candidates have a durable queue and cursor; per-run publication claims
and Discord nonces prevent repeat alerts. Ambiguous sends are marked `needs_review`
and are not retried automatically. Record processing has a separate lease and does
not delay collection or raid announcements. A new season has independent records.
# Pinned dungeon records

`MPLUS_RECORD_BOARD_ENABLED=1` maintains one pinned image in the configured
Mythic+ channel. The card uses Inter, the existing dark palette, green key levels,
and WoW class colors for guild record holders. Each panel places dungeon artwork
from Raider.IO's season metadata on the left; bounded image downloads are cached
within the worker process. A style version forces an image refresh after layout
changes without waiting for a new record. It shows current-season records
for timed groups with at least two guild characters; higher keys win, then faster
times at the same level. A new record refreshes the existing message before its
announcement links back to the pin. Historical discoveries refresh the card
quietly. Content-addressed image URLs prevent stale Discord image caching.

The message ID and fingerprint live in `RECORD_BOARD#<channel>` under the
existing guild partition. Pin failures retry against that same message; an
ambiguous initial send requires review rather than risking duplicate cards.
The bot requires channel posting, embed and pin permissions. Existing weekly
recap scheduling is unchanged.

The pin links to `/mplus/records/` on the recap site. Each record update publishes
the card and matching accessible HTML standings before updating Discord. The page
includes dungeon run source links, class-colored names with light/dark contrast,
and refreshes once per minute; HTML cache lifetime is 60 seconds.

Role icons appear immediately before character names on cards and web standings.
For highest-key standings and dungeon records, the icon comes from the selected
run's roster specialization: tank, healer, or damage. Switching roles in another
run cannot change that record's icon. Record alerts label both the new and previous
groups using their respective runs. Aggregate IO and run-count standings use the
character's highest positive tank, healer, or damage IO score at the reporting
cutoff. Equal scores prefer damage, then tank, then healer. Role scores are stored
with each score observation. Without role scores, Mage, Rogue, Hunter, and Warlock
can safely show damage; unknown hybrid roles remain blank. Historical recaps with
no archived role breakdown may use a separately labelled current-season observation
for icons only, without changing their historical scores or rankings.
