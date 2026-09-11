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
- Timed runs: qualifying timed completions per character.
- Timed +10 runs: qualifying timed completions at level 10 or higher per character.
- All-guild highest: highest timed runs whose entire five-character party is guild.
  Show each complete party on the website.
- All-guild runs: each character's timed completions in full-guild groups.
- Alts remain separate characters; no unverified Discord/account-name matching.
- Equal values share competition rank; deterministic name ordering breaks display
  ties. The Discord card shows exactly up to three entries; the website shows all.

## IO calculation

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
