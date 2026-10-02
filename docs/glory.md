# Raid meta achievement

greyBot watches one raid meta achievement per install ("Glory of the Venomous Raider",
id 63254, this tier) **for the guild** and posts:

- one blue card when the guild earns each achievement listed under it: the achievement and
  "N of 8 toward" the meta;
- one gold card when the guild has earned all of them.

No raider is named and nothing pings.

## What "the guild earned it" means

Blizzard keeps no guild record of these. Its guild achievements are the Guild Runs (boss
kills in a guild group); the Glory achievements exist only on characters. So it is derived:
**ten or more watched raiders earning the same achievement within a minute of each other**
is a guild group doing it together. One raider getting it in a pug counts for nothing, and
is simply not one of the ten when the guild does it. `group` in the setup changes the ten.

The gold card follows the eighth blue one. It does not wait for any single raider to hold
the meta and its mount.

## Rules and limits

- The only source is Blizzard's character achievements profile, a snapshot taken when the
  character logs out. What each raider holds is kept between runs, so the group fills in
  as people log out and the card posts on the run that sees the tenth.
- Watched members are the install's `ROLLCALL#SETUP` mapping with the `VAULT#CHARACTERS`
  locks applied, narrowed to one role's holders when the setup names a role. Every mapped
  character is read: the achievements are account-wide, but an alt that has not logged in
  since does not show them. A main and an alt are one raider.
- Realms come from the guild's Raider.IO roster, else the guild's own realm. A character
  Blizzard will not answer for is logged as `glory_character_missing` and skipped.
- The achievements document is 1 to 2 MB per character. The 4 KB character summary's
  achievement points and last login are stored as a signature; an unchanged one skips the
  download.
- State is one `GLORY#<meta id>` row per install: the announced set, claimed before each
  post and released if Discord refuses it, plus who holds what and the signatures.
- **The first live run seeds.** Whatever the guild has already earned is recorded and not
  announced. `{"mode":"glory","backfill":true}` on that first run announces it instead.
- A new tier is a new meta id: save the setup again with it. The old row stays.

## Running it

| event | effect |
|---|---|
| `{"admin":"glory_setup","team":"prog-raid","achievement":63254,"live":false,"role":"<role id>","channel":"<channel id>","group":10}` | save the setup; `role`, `channel` and `group` optional; the row is replaced whole |
| `{"mode":"glory","dry":true}` | how many raiders hold each, what the guild has earned, what is pending; no writes, no posts |
| `{"mode":"glory"}` | the check; posts only where the setup is `live` |

`ryangrey-greybot-glory` sends `{"mode":"glory"}` every 30 minutes. With no setup row, or
`live:false`, that run makes no Blizzard calls and posts nothing.

## Going live

1. Deploy (`scripts/deploy-cdk.sh prod`). Nothing posts: there is no setup row.
2. Save the setup with `live:false` and run `{"mode":"glory","dry":true,"team":"prog-raid"}`.
3. Save it again with `live:true`. The next run seeds silently, or announces what the
   guild has already earned if invoked once by hand with `"backfill":true`.
