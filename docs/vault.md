# Tuesday vault and gear check

Every Tuesday at 11:30 AM Eastern, after the US weekly reset, greyBot posts one card to an
officers-only channel under Progression Raid. Each Prog Raider gets one row:
Discord name → character, then four columns.

| column | what it shows | source |
|---|---|---|
| Raid | vault slots from unique Heroic or Mythic bosses, 2 / 4 / 6 | Blizzard encounter profile ∪ guild Warcraft Logs kills |
| M+ 10+ | the key level each of the three slots pays out at (1st / 4th / 8th best run); green at +10 | Raider.IO weekly runs, filtered to reset → reset |
| Gems | empty sockets, and gems below the season's top rank | Blizzard equipment + item data |
| Enchants | missing enchants, and enchants below max rank | Blizzard equipment |

A raider is **short** when fewer than two Mythic+ slots reached +10 (fewer than four runs
at +10 or higher). The post's text repeats every raider with something to fix and names
the slots behind each count. Mentions are suppressed, so it pings nobody.

## Rules and limits

- The vault week runs reset to reset (Tuesday 15:00 UTC). Gear is read as equipped when
  the report runs.
- Prog Raiders are members holding the prog role. Their characters come from the prog-raid
  roll call mapping (`ROLLCALL#SETUP`). A member with several characters is reported on the
  one that raided with the guild most that week, then the highest item level.
- Raider.IO only counts runs it has seen. An amber "RIO <date>" means the character had not
  been refreshed for over a day before reset.
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

## Go live (in this order, only when approved)

1. Deploy the code: `scripts/deploy-cdk.sh prod` (done 2026-09-23). The deploy grants
   `/greybot/vault/channel_id`; nothing posts yet. CDK leaves the Lambda's Mythic+
   environment variables alone as long as the stack's own Environment block is unchanged.
   Diff the environment and schedules before and after. Do not "restore" them with
   `infra/configure-mplus.py --apply`, which also rewrites the Mythic+ schedules.
2. Create the channel: `scripts/create-vault-channel.py` (preview), then `--apply`. It
   denies @everyone and allows only greyBot; GM and Officer see it through Administrator.
3. `aws ssm put-parameter --name /greybot/vault/channel_id --type String --value <id>`.
4. `infra/create-vault-schedule.sh` (preview), then `--apply`.

Turning it off is deleting the schedule or emptying the parameter.
