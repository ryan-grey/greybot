# discord-mcp

A local [MCP](https://modelcontextprotocol.io) server that gives the Claude
desktop app one searchable index over your Discord — and is honest about
which parts of it can be read, and which can be written.

Discord's API does not let one thing see everything. A bot sees only the
servers it has been invited to and can never read your DMs. The only way to
see *everything* is to automate your own user account, which Discord forbids
and can terminate your account for. This project does not do that, not as an
option and not "carefully". Instead it combines the sources that are allowed,
tags every message with where it came from, and refuses cleanly where there is
no compliant path.

| Source | What it covers | Reads | Writes |
| --- | --- | --- | --- |
| `bot` | servers a bot you own has been invited to | REST, incremental by snowflake | **yes** — send / edit / delete *as the bot* |
| `export` | Discord's official data-request package | one-time backfill | never |
| `local` | an optional passive capture database ([discord-local-log](https://github.com/ryan-grey/discord-local-log)) | read-only | never |

Everything is local: a SQLite + FTS5 index in `~/.local/discord-mcp/index.db`,
and an append-only `discord-changes.log` next to it recording the before and
after of every write. Nothing is hosted; the only network calls are to
Discord's REST API with the bot's token.

## What the export does and does not contain

The data package (Discord → Settings → Data & Privacy → Request my data) holds
**only the messages you sent** — in every server and every DM, back to the
beginning. Other people's messages are not in it. So after `import_export` you
can search your own words everywhere, and read your side of any DM, but not
the replies. That is a Discord limitation, not a bug here.

The honest coverage picture:

| You want | Path | Reality |
| --- | --- | --- |
| Your own messages, everywhere, all history | export | complete, zero risk |
| Other people's messages in servers you admin | bot | full history via REST |
| Other people's messages in servers you don't admin | none | you cannot add a bot there |
| DMs, both sides | none | no bot can read DMs |
| Send / edit / delete as the bot | bot | in servers the bot is in |
| Send / edit / delete as *yourself* | none | requires automating your account |

## Tools

Read (all sources; every row carries `source`):

| Tool | What it does |
| --- | --- |
| `search_messages(query, guild?, channel?, author?, since?, until?, source?, limit=50)` | Full-text search, newest first. Ids match exactly; names match as substrings. |
| `get_channel(channel_id, since?, until?, limit=200)` | Ordered messages for one channel, by id or exact name. |
| `get_dm(handle_or_user, since?, until?, limit=200)` | A DM or group DM by username, name, or participant id. Export/local only. |
| `list_guilds()` | Every server the index knows, its source, and message counts per source. |
| `list_channels(guild?)` | Channels for one server, or all DMs when empty. |
| `index_status()` | Per-source counts and newest message, whether the bot token works, whether the Message Content intent is on, and which servers the bot is in. |

Sync and import:

| Tool | What it does |
| --- | --- |
| `sync_bot(guild?, full=false, max_pages=20)` | Pull new messages from the bot's servers. Incremental; `full=true` re-reads from the start. |
| `import_export(zip_path)` | Load the official data package as `source=export`. Idempotent; never overwrites rows the bot already saw. |
| `sync_local()` | Pull from the optional local capture database, if present. |

Write (bot only, every call needs `confirm=true`, every call is logged):

| Tool | What it does |
| --- | --- |
| `send_message(channel_id, text, confirm)` | Send as the bot. |
| `edit_message(channel_id, message_id, text, confirm)` | Edit one of the bot's own messages. |
| `delete_message(channel_id, message_id, confirm)` | Delete a message the bot may delete. The index keeps the row flagged `deleted`. |

A write to a channel known only from the export or local source is refused
with a message that says why — there is no permitted way to send, edit or
delete as your own account — rather than failing obscurely.

Messages come back as `id`, `source`, local ISO `time`, `guild`, `channel`,
`author`, `content`, `attachments` (names and URLs only; nothing is
downloaded), `edited` and `deleted`.

## Setup

```sh
python3 -m venv ~/.local/discord-mcp/.venv
~/.local/discord-mcp/.venv/bin/pip install mcp
```

Register in `~/Library/Application Support/Claude/claude_desktop_config.json`
and restart the Claude desktop app:

```json
"discord": {
  "command": "/Users/<you>/.local/discord-mcp/.venv/bin/python",
  "args": ["/Users/<you>/Developer/greybot/integrations/discord-mcp/server.py"],
  "env": {"DISCORD_BOT_TOKEN_SSM": "/discord-mcp/bot-token"}
}
```

The bot token is read from `DISCORD_BOT_TOKEN`, or — as above — from the AWS
SSM parameter named by `DISCORD_BOT_TOKEN_SSM` (SecureString, read with the
`aws` CLI using profile `DISCORD_AWS_PROFILE`, default `infra`, region
`AWS_REGION`, default `us-east-1`). Either way it is never written to disk by
this code and never returned by any tool. Without a token, everything except
`sync_bot` and the write tools still works.

### Bot setup (Discord Developer Portal)

1. Create a new application, e.g. **Grey Reader**, at
   <https://discord.com/developers/applications>. Use a dedicated app; do not
   reuse a bot that already does something else.
2. **Bot** page → Privileged Gateway Intents → enable **Message Content**.
   Without it the bot connects fine and every message indexes with empty text
   — the documented silent failure. `index_status` reports the intent state
   so this cannot go unnoticed. Enable **Server Members** only if you want
   member lookups later.
3. **Bot** page → Reset Token → copy it once. Put it in SSM as a SecureString
   (or in the `env` block above as `DISCORD_BOT_TOKEN`).
4. **OAuth2 → URL Generator** → scope `bot` → permissions **View Channels**
   and **Read Message History**, plus **Send Messages** / **Manage Messages**
   only if you want the write tools. Open the generated URL and invite the bot
   to each server you administer.
5. In the Claude desktop app: `index_status()` to confirm the token and
   intent, then `sync_bot()`.

### Data export

Discord → Settings → Data & Privacy → Request my data. The ZIP arrives by
email within a few days. Then `import_export("/path/to/package.zip")`.

## Tests

```sh
~/.local/discord-mcp/.venv/bin/pip install pytest
~/.local/discord-mcp/.venv/bin/python -m pytest tests/
```

Fixtures are fully synthetic: a hand-built export ZIP and a fake REST
adapter. No real Discord data is in the repository. The suite covers search
across mixed sources, incremental sync, refusal without `confirm`, refusal on
non-bot channels, import idempotency (a re-import adds nothing and never
overwrites bot rows), the change log, and the intent check.

## License

MIT.
