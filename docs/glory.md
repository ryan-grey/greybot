# Raid meta achievement

greyBot watches one raid meta achievement per install ("Glory of the Venomous Raider",
id 63254, this tier) and posts:

- one blue card the **first time anyone on the team** holds each achievement listed under
  it: the achievement, who was first, and "N of 8 toward" the meta;
- one gold card the first time anyone holds **the meta itself**.

Never one card per raider, and nothing pings. A raider who holds the meta holds all of its
parts, so any parts not yet announced go out first, oldest first, and the meta last.

## Rules and limits

- The only source is Blizzard's character achievements profile, a snapshot taken when the
  character logs out. A card can trail the kill by however long people stay logged in, and
  it names whoever was visible when the achievement was first seen.
- Watched members are the install's `ROLLCALL#SETUP` mapping with the `VAULT#CHARACTERS`
  locks applied, narrowed to one role's holders when the setup names a role. Every mapped
  character is read: the achievements are account-wide, but an alt that has not logged in
  since does not show them. One person is one name on a card.
- Realms come from the guild's Raider.IO roster, else the guild's own realm. A character
  Blizzard will not answer for is logged as `glory_character_missing` and skipped.
- The achievements document is 1 to 2 MB per character. The 4 KB character summary's
  achievement points and last login are stored as a signature; an unchanged one skips the
  download.
- State is one `GLORY#<meta id>` row per install: the announced set, claimed before each
  post and released if Discord refuses it, plus the signatures. Signatures are not saved
  on a run with a failed post, so the retry reads the character again.
- **The first live run seeds.** Whatever is already earned is recorded and not announced.
  `{"mode":"glory","backfill":true}` on that first run announces it instead.
- A new tier is a new meta id: save the setup again with it. The old row stays.

## Running it

| event | effect |
|---|---|
| `{"admin":"glory_setup","team":"prog-raid","achievement":63254,"live":false,"role":"<role id>","channel":"<channel id>"}` | save the setup; `role` and `channel` optional; the row is replaced whole |
| `{"mode":"glory","dry":true}` | what is earned, announced and pending; no writes, no posts |
| `{"mode":"glory"}` | the check; posts only where the setup is `live` |

`ryangrey-greybot-glory` sends `{"mode":"glory"}` every 30 minutes. With no setup row, or
`live:false`, that run makes no Blizzard calls and posts nothing.

## Going live

1. Deploy (`scripts/deploy-cdk.sh prod`). Nothing posts: there is no setup row.
2. Save the setup with `live:false` and run `{"mode":"glory","dry":true,"team":"prog-raid"}`.
3. Save it again with `live:true`. The next run seeds silently, or announces what is
   already earned if invoked once by hand with `"backfill":true`.
