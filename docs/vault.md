# Tuesday vault and gear check

Every Tuesday at 11:30 AM Eastern, after the US weekly reset, greyBot posts one card to an
officers-only channel under Progression Raid. Each Prog Raider gets one row:
Discord name → character, then five columns.

| column | what it shows | source |
|---|---|---|
| Raid | observed minimum slots from unique Heroic or Mythic bosses, 2 / 4 / 6 | Blizzard encounter profile ∪ guild Warcraft Logs kills |
| M+ 10+ | observed minimum slot levels (1st / 4th / 8th best run); green at +10, ? for unverified | highest +10 slot count from the three independent providers |
| Source | only the provider selected for that character | Raider.IO, Blizzard, or Warcraft Logs |
| Gems | empty sockets, and gems below the season's top rank | Blizzard equipment + item data |
| Enchants | missing enchants, and enchants below max rank | Blizzard equipment |

A raider **needs verification** when fewer than four completed +10 runs are visible.
Missing public runs cannot establish a failed requirement. Every count is a lower bound;
an unconfirmed slot is shown as **?**, never as definitely empty. The text includes raiders
with every character's selected source and observed +10 slot count.
Mentions are suppressed, so it pings nobody.

Edited/reposted cards must show a dated **What changed** section on the image and in
the message text. `revision={"date":"Sep 30, 2026","changes":["Specific correction"]}`
is supported by the renderer, message payload, and manual vault events. Preserve earlier
correction notes and the actual data check time; annotation-only edits reuse the posted
snapshot. See `AGENTS.md` for the policy across all recap cards.

## Rules and limits

- The vault week runs reset to reset (Tuesday 15:00 UTC). Gear is read as equipped when
  the report runs.
- Prog Raiders are members holding the prog role. Shared character/alt mappings remain in
  `ROLLCALL#SETUP` for attendance and Saturday raid information. Vault-only prog-character
  locks live separately in `VAULT#CHARACTERS` under the same tenant partition, with a
  JSON-encoded `members` attribute mapping Discord IDs to one character name each.
  Locks apply before any character profile or provider lookup: alts are not candidates,
  even if they have more kills, higher gear, or more vault slots. Missing prog-character
  data stays unavailable; it never falls back to an alt. Invalid/unreadable lock records
  stop the check rather than silently ignoring the locks. Unlocked members retain the
  previous most-raid-kills, then highest-item-level selection.
- The role icon is the role raided that week (Warcraft Logs playerDetails over the week's
  Heroic and Mythic pulls, most pulls wins), else Blizzard's live active spec. Raider.IO's
  active spec is the last resort because it lags: it had a Protection paladin in his tank
  set down as Retribution.
- Blizzard shows only the set being worn. A raider logged out in a spec of a different role
  from the one they raided as gets "<spec> gear / not checked" instead of grades, and a
  "gear not checked" line in the post.
- Raider.IO only counts runs it has seen. A recent crawl does not certify completeness.
  Stale dates appear in the text even for raiders meeting the minimum; the card includes
  them where space permits. The post records when the check ran. Run IDs prevent duplicate
  counting across the current/previous-week lists, and untimed completions still count.
- Each provider is counted independently for the same character and reset window. Most
  +10 slots wins; ties prefer more +10 runs, then Raider.IO, Blizzard, Warcraft Logs in
  that fixed order. The displayed slot levels all come from the selected source.
- Blizzard supplies current-period and season best runs, with seasons discovered by date.
  The hourly collector also discovers weekly periods and active dungeon leaderboards for
  each prog character's connected realm. Matching party members are checked by name and
  realm; profile and leaderboard observations share one Blizzard run history. Dungeon ID
  plus completion second prevents overlap between these endpoints from counting twice.
  These lists are partial, not complete vault ledgers. Duplicate runs across its endpoints
  count once. Warcraft Logs discovers paged character reports as well as guild/team logs;
  full completed dungeon runs count, including untimed completions, but raid bosses and
  abandoned/in-progress runs do not. Name plus realm must match. Overlapping uploads for
  the same character, dungeon and key level within 60 seconds count once.
- Source failures do not suppress the other providers. Dry-run rows retain all provider
  counts for diagnosis; the published card and text name only the selected provider.
  Warcraft Logs discovery is bounded to five 100-report pages per character, and only
  reports accessible with the configured account grant can be read. No addon is required.
- Raider.IO history includes previous/current weekly lists, recent runs, and season-highest
  runs. Repeated observations merge by run ID or dungeon/completion time. Warcraft Logs
  uses dungeon, level, and a one-minute clock tolerance to deduplicate overlapping uploads.
- `ryangrey-greybot-vault-collect` runs hourly at minute 10 UTC in its own five-minute
  Lambda, with conditional writes protecting overlapping executions. Failures are recorded
  in its CloudWatch logs and the collector's latest successful timestamp remains in history.
  It collects the current and previous reset weeks, checkpoints each provider, and never
  posts to Discord. Unlocked ambiguous mappings are skipped by the collector until given
  a prog-character lock; all 19 current prog members resolve without ambiguity.
- History is private in the separate `ryangrey-greybot-vault-history` table. Keys include
  tenant, region, realm, character, and week ending date. Provider histories remain separate;
  only runs within a single provider are combined. Conditional version writes preserve
  concurrent observations. Smaller responses and outages cannot erase saved runs. Retention
  is 90 days after that week's reset via TTL on this new table only; the bot's dedupe table
  is unchanged. Dry runs read history without writing it. Tuesday's recap merges saved and
  fresh evidence, then displays the provider with the highest qualifying slot count.
- Collection approach adapted from [WoWAudit's MIT-licensed collector](https://github.com/wowaudit/core/blob/89981788fe8b903d264ee4f6acec0529793234a6/lib/wowaudit/retrievers/keystones.rb).
  Its copyright/license notice is retained in `assets/LICENSE-wowaudit.txt` and bundled in
  both deployed Lambda packages. greyBot collects provider data directly, not from the
  WoWAudit guild page. Leaderboards cannot expose every run; missing data remains unknown.
- Blizzard keeps only the last kill per boss, so a boss killed again after reset hides its
  earlier kill. The Tuesday-morning run happens before any raid; a later re-run still counts
  guild-logged kills through Warcraft Logs.
- Enchant slots (`vault.ENCHANT_SLOTS`) are Midnight's: head, shoulders, chest, legs, feet,
  rings, weapon, and a weapon off hand. They change with the expansion.
- The top gem rank is read from the team's own gems (item level 295 in Season 2), so a new
  season needs no edit.
- The Corrosive Codex is deliberately not tracked: it is inactive in raids and dungeons, and
  the raid rules do not require it.

## Running it by hand

```json
{"mode":"vault","dry":true}       // rows only, nothing drawn or sent
{"mode":"vault","preview":true}   // the card DM'd to /greybot/recap/low_parse_dm only
{"mode":"vault","end":"2026-09-22"}  // a specific week, with the weekly-post rules
```

The weekly post claims its week (`VAULT` row, tenant partition) before sending and never
releases it: a missed post can be sent by hand, a duplicate cannot be taken back.

## Go live (done 2026-09-23)

1. Deploy the code: `scripts/deploy-cdk.sh prod`. The deploy grants
   `/greybot/vault/channel_id`.
2. Create the channel: `scripts/create-vault-channel.py` (preview), then `--apply`. It
   denies @everyone and allows only greyBot; GM and Officer see it through Administrator.
3. `aws ssm put-parameter --name /greybot/vault/channel_id --type String --value <id>`.
4. The schedule, first made by the retired `infra/create-vault-schedule.sh`, is now
   `VaultSchedule` in the CDK stack (`cdk/greybot/config.py`), adopted 2026-09-24.

Turning it off is emptying the parameter; removing `VaultSchedule` from the stack and
deploying stops the timer itself.

## Accuracy follow-up

On September 30, a read-only production rerun for September 22–29 selected Teradiir and
returned eight +10-or-higher runs, with slot levels +15 / +12 / +10. This is consistent with
the reported two-slot card having used data before the final run appeared. The original raw
profile was not retained, so its exact missing run and crawl time cannot be reconstructed.

The hourly collector now retains private run evidence and rechecks both the current and
previous weeks. This improves coverage but cannot recover a run no source ever exposes.
The Tuesday recap consumes this history at posting time. Automatic edits to an already
posted recap and preservation of its original gear snapshot are separate future work;
the collector does not send or edit Discord messages.
