# discord-local-log

A capture-only [Vencord](https://vencord.dev) plugin plus a small ingest
script. It records the messages your Discord desktop client has **already
received and rendered** — DMs, group DMs, and every server you can see — into
a local SQLite database that [discord-mcp](https://github.com/ryan-grey/greybot/tree/main/integrations/discord-mcp)
reads as its `local` source.

It never sends, never edits, never deletes, never calls Discord's REST API,
and never fetches anything with your token. It listens to the client's own
Flux events and writes what it hears to a file. Scrolling back in a channel
backfills history as you browse, because the client loads those messages
anyway. That distinction is the whole safety argument.

**The risk, stated once.** Vencord is a client modification, and client
modification is against Discord's Terms of Service. Passive capture with no
outbound calls is the lowest-exposure form of it — there is no traffic that
would not have happened anyway — but lower risk is not no risk, and
enforcement is at Discord's discretion. Decide for yourself before installing.

## How it fits together

```
Discord.app ──(patched to load a local Vencord build)──▶ DiscordLocalLog plugin
    renderer: MESSAGE_CREATE / UPDATE / DELETE / DELETE_BULK / LOAD_MESSAGES_SUCCESS
    main:     appends JSON lines to ~/Library/Application Support/discord-local-log/events.jsonl
                                             │
launchd (on file change, or every 5 min) ──▶ ingest.py ──▶ discord.db
                                                              │
                                    discord-mcp sync_local() reads it (source=local)
```

- `plugin/discordLocalLog/` — the Vencord user plugin. `index.ts` runs in the
  renderer and batches events; `native.ts` runs in Electron's main process and
  appends them to the file (the renderer has no filesystem access, and no
  CSP exception is needed because nothing goes over the network).
- `ingest.py` — stdlib Python. Reads `events.jsonl` from a saved offset,
  applies creates / backfills / partial updates / deletes to `discord.db`,
  rotates the file once it is large, and drains any lines written during the
  rotation. Every change bumps a `rev` counter so a reader can pull edits and
  deletes, not just new ids.
- `tools/patch_discord.py` — points `/Applications/Discord.app` at a local
  Vencord build the same way Vencord's installer does (rename `app.asar` to
  `_app.asar`, write a two-file `app.asar` that requires `dist/patcher.js`).
  `--unpatch` restores the original; `--status` shows which is in place.
- `launchd/com.ryangrey.discord-local-log.plist` — runs the ingest whenever
  the events file changes.

## Backfilling a whole conversation

Scrolling to the top of a DM records its full history, because the client
loads every page on the way. For long conversations there is a chat-bar
button (the box icon next to the emoji picker): click it and the plugin asks
the client to load the conversation 100 messages at a time, pausing between
pages (`backfillPaceSeconds` in the plugin settings, default 1.5 — Discord
allows roughly one request a second per conversation; set 3-6 to look like
a person scrolling), until it reaches the top. Right-click the button to
queue every DM in the list, run one after another, skipping ones already
complete. A 20,000-message conversation takes about five minutes at the
default pace. While it runs, a percent and
estimated time remaining sit just left of the button; click again to stop.
Discord does not need to be focused or even visible while it runs: the wait
between pages happens in Electron's main process, which Chromium does not
throttle when the window is minimized (renderer timers are).
Stopping is safe: the point reached is saved per conversation, so the next
click continues from there even after switching channels or restarting
Discord (pages seen twice are simply upserted, never duplicated).
A toast reports the total when it finishes. The estimate is time-based —
Discord never says how many messages a conversation holds, so it uses how
much calendar time each recent page covered against how far back the DM's
creation date is — rough for the first few pages, steadier after that.
A conversation that reached the top gets a green check on its entry in the
DM list (and "History fully captured" in the button's tooltip); the marks
persist across restarts in Vencord's DataStore.

Be clear about what this is: the requests are the same ones scrolling makes,
but a script is issuing them. That is a step past passive capture, and it is
the reason the button is opt-in per conversation rather than automatic.

## discord.db schema

```
channels(id, guild_id, guild_name, name, type, recipients)
messages(id, guild_id, channel_id, author_id, author_name, content,
         ts, edited_ts, attachments, deleted, rev)
```

Timestamps are unix milliseconds UTC. A message deleted before it was ever
seen is kept as a tombstone with its channel and the time from its snowflake.

## Setup

Vencord's installer only ships the official plugin set; a user plugin needs a
build from source.

```sh
# 1. Toolchain
npm i -g pnpm

# 2. Vencord source, outside this repo, with the plugin copied in
#    (copied, not symlinked: esbuild resolves Vencord's path aliases from
#    a file's real location, so a symlink outside src/ does not build)
git clone --depth 1 https://github.com/Vendicated/Vencord.git ~/.local/discord-local-log/Vencord
mkdir -p ~/.local/discord-local-log/Vencord/src/userplugins
cp -R plugin/discordLocalLog ~/.local/discord-local-log/Vencord/src/userplugins/
cd ~/.local/discord-local-log/Vencord && pnpm install --frozen-lockfile && pnpm build

# or simply: tools/install.command does steps 1-4 in one go

# 3. Patch Discord (quit it first)
python3 tools/patch_discord.py --build ~/.local/discord-local-log/Vencord

# 4. Ingest on change
cp launchd/com.ryangrey.discord-local-log.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ryangrey.discord-local-log.plist
```

Open Discord, then Settings → Vencord → Plugins → enable **DiscordLocalLog**.
Its settings let you turn off DMs or servers, and list channel ids to skip.

When Discord updates itself the patch is undone (the updater replaces the
bundle). Re-run step 3. `pnpm build` again after pulling Vencord.

To remove everything: `python3 tools/patch_discord.py --unpatch`, unload the
launch agent, delete `~/Library/Application Support/discord-local-log`.

## Tests

```sh
python3 -m pytest tests/
```

Synthetic events only. Covers create/backfill/partial update/delete/bulk
delete, tombstones, offset resume across a half-written line, rotation with
late lines, the asar round-trip, and patch/unpatch on a fake bundle.

## License

GPL-3.0. The plugin compiles into [Vencord](https://github.com/Vendicated/Vencord),
which is GPL-3.0, so the whole repository carries the same license.
