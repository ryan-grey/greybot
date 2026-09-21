# Server control service

This additional service is under development. It does not replace the deployed
raid Lambda, register commands, change bot permissions or remove integrations.
It uses the same bot application for Gateway collection and REST moderation;
the existing signed `/progress` HTTP endpoint remains in place.

Implemented: Discord administrator login, private event history, optional local
message indexing, a read-only guild-index importer/search CLI, prepared settings,
and audited timeout/kick/ban requests, persistent mutes, automatic spam moderation,
filtered audit posts, arrival/departure messages, signed self-role buttons and member
verification. Dashboard is the landing page, followed by Needs Review, event history,
message search, moderation, settings and the activity scoreboard. Administrators can
review a member selection filtered by role or server display name and queue an
explicit add/remove operation; each member is authorized again by the worker.
Production actions default off and require an archive. Invite attribution,
achievements and additional utility commands are not implemented.

Discord audit-feed settings are independent of private event collection. The
Settings page can prepare a destination, individual event selections, ignored
channels, bot-action filtering and member avatars. Its default preset excludes
voice joins/leaves and includes the other listed events. These filters never
remove records from the admin history or archive. Posting requires explicit activation
and verified archive receipts; enabling a feed starts from the current event cursor.

Admin forms select members and channels by name; member choices include server
avatars and server display names, and channel choices include their category. Expanded
records translate member, role, channel and server references without changing
the underlying journal. A guild-scoped display directory caches names for five
minutes and keeps last-known labels for departed members or deleted resources.
Unknown references are labeled unavailable rather than inventing a name. This
cache is display-only: every private request and moderation operation still
uses live authorization. All raw identifiers remain internal to API requests
and storage rather than user-facing entry fields.

Needs Review records administrator acknowledgements without changing permissions.
It surfaces the retired Co-GM role, category permission differences and members who
can see a channel without holding any role explicitly allowed by that channel.
These are possible access exceptions, not automatic findings of unauthorized access;
administrators and the server owner are excluded from those membership suggestions.

The activity scoreboard includes current human members, including zero activity,
and uses captured guild messages, reaction additions and observed voice minutes.
Each contributes one point per message, reaction or minute. Imported guild messages
are included, but missing history and voice time across disconnects are not estimated.
Private messages and other guilds are excluded by collection and search boundaries.

The health dialogue identifies greyNAS as the host and reads the Linux host uptime
through `/proc/uptime`. Worker samples are recorded every 30 seconds; observation
gaps over 90 seconds and boot-time changes create journal entries with last-seen and
recovery times. These are bounded monitoring gaps, not exact outage start times;
historical outages before this monitor existed cannot be reconstructed.

The member-only `/channels` page uses a separate OAuth session that grants no admin
access. It offers eligible text and voice channels to verified members or members
with higher server roles. Hiding sets only a member View Channel deny; showing removes
that owned deny after checking current underlying access. It never adds a View allow,
offers inaccessible channels, or changes role membership. Rules, welcome and channel
preferences remain available. Discord administrators bypass channel visibility denies.

## Voice clips

The whole workflow happens inside Discord with one command that takes nothing to fill
in, written so a child can use it. The first `/clip` in a voice channel brings greyBot in
to listen. Every `/clip` after that grabs the 5 seconds ending at the last sound (Discord
allows 5.2, and MP3 padding pushed a 5.2-second clip over the limit in a live upload), not
the quiet spent typing the command, and answers privately with
a playable `clip.mp3` and four buttons: **✅ Add to soundboard**, **⏪ A bit earlier**,
**⏩ A bit later** and **🗑️ Throw away**. Earlier and later slide the clip two seconds
within the 30 seconds that were kept and answer with the new clip. The green button asks
one question, "What should we call it?", and the answer goes straight onto the server
soundboard. The listening reply carries a **👋 Stop listening** button. Nobody types a
number, and every reply is ephemeral, so only the member sees it.

The interactions Lambda verifies and relays `/clip` and `greybot:clip:` components to
`/discord/roles` like raid signups (`src/handler.py`, `src/role_relay.py`), and registers
the command from `src/raid_commands.py`, which a test keeps equal to
`voice_clips.commands()`. The service answers within Discord's deadline with a private
deferred reply (type 5, which the relay now accepts) or the form, and queues an audited
`voice_*` job. The worker, which holds the voice connection, finishes the reply through
the interaction webhook. Interaction tokens are credentials: they live in `voice_tokens`
beside the queue for at most 14 minutes, are deleted on first use, and never enter the
job body or the audit journal.

Discord allows a bot account one voice connection per server, so each simultaneous voice
channel needs its own account. greyBot always takes the first channel. Optional helper
bots, configured as `GREYBOT_VOICE_HELPER_1_TOKEN_SSM`, `_2_` and so on up to 8, take the
next ones: the worker logs each in with only the guilds and voice-state intents and adds
it to the pool. `/clip` still goes to greyBot, which posts the notice; whichever account is
free joins, and a member's clip always comes from the bot in their own channel. When every
account is busy the member is told greyBot is busy. A helper needs View Channel and
Connect in voice channels and nothing else, and must be a different application from
greyBot and from the other helpers. With no helpers configured greyBot clips one channel.

Any verified human member may use it, but only for the voice channel Discord reports
them connected to when the job runs. Nobody, administrators included, can start
listening to or capture a channel they are not in; greyBot holds one connection, so a
second channel is refused while it is listening elsewhere. Members name, publish and
discard only their own captures, keep at most 5 drafts and publish at most 3 sounds a
day, because soundboard slots are shared by the server. Administrators can act on any
capture, have no limits and can always ask greyBot to leave. Refusals are explained to
the member in the private reply.

Joining posts a fixed notice in the voice channel's chat first and refuses to connect if
that post fails, so nobody is recorded without an announcement. On joining it also plays
`assets/voice/listening.wav` ("greyBot is now listening for slash clip commands", a
48 kHz mono WAV Ryan supplied and approved for this public repository) and then mutes
itself; a missing or unplayable file skips the spoken line, never the posted notice.
greyBot stays undeafened and stays in the channel while anyone is there. It leaves, and
forgets the channel, when the last person leaves or someone presses Stop listening. After
a worker restart or a lost connection it returns to a channel that still has people in it
and posts the notice again; three failed returns make it give up rather than post notices
forever. Received audio is transport-decrypted by
`discord-ext-voice-recv`, then DAVE end-to-end decrypted per speaker with the `davey`
session discord.py already negotiates, decoded, and mixed on a shared clock into a rolling
90-second buffer held in memory only. Frames that cannot be decrypted are dropped and
counted in `voice_status` rather than buffered as noise.

A clip writes the 30 seconds before the command to `voice-captures/` in the state directory
as a WAV, kept only so the member can trim it. It is deleted on discard, on publish, and
otherwise within an hour. Publishing trims the chosen selection, encodes MP3 and creates
a guild soundboard sound under the member's name, within Discord's 512 KiB limit; anyone
can then play it from the soundboard in voice. The capture is marked `publishing` before
the upload; an ambiguous upload stays there for manual review and is never retried, so a
sound cannot be duplicated. A refused upload, such as a full soundboard, returns the
capture to draft.

greyBot needs View Channel, Send Messages and Connect in the voice channel, and Create
Expressions in the server. The Docker image adds `libopus0`. `requirements-voice.txt` is
installed with `--no-deps` because that package's `discord.py[voice]` extra caps PyNaCl
below the pinned release. Going live needs three steps in order: deploy this service,
deploy the Lambda, then re-register the guild commands.

Three library faults were found live on 2026-09-18 and are worked around in
`voice_clips.py`, each with a comment and a test; revisit them when the pins in
`requirements*.txt` move. discord.py 2.7.1 arms its handshake state only after sending the
join, and its state waiter signals with `Event.set()` then `Event.clear()` at once, which
loses the wakeup when Discord's two replies land a millisecond apart: on the NAS worker
every join timed out after 20 s until the state was armed first and polled. And
`discord-ext-voice-recv` 0.5.2 discards a member's audio after they leave the channel until
Discord re-announces them, which it did not when they came back; greyBot lifts that block
and identifies an unannounced stream by whose DAVE key opens it. The worker logs voice
connection progress, the type of any job failure and counts of unusable audio, never
payloads.

Verified in production that day: join with the spoken intro, clip, earlier, naming, upload
(a 5.04 s 48 kHz MP3 on Discord's CDN) and a returning speaker. Simultaneous channels with
the helper bots have only been exercised in tests.

## Voice activity

`/activity` is a live, members-only page: who is in voice and for how long, ranked, with
who is in voice right now. It uses the same member OAuth session pattern as `/raids` and
`/channels`, checks current human membership on every request, grants no admin access and
is marked `noindex`. `voice_activity.compute` walks the journal's `VOICE_STATE_UPDATE`
events. The server's AFK channel never counts, a stay is one unbroken visit of at least a
minute, and mute or deafen updates do not split a stay. An observation gap ends every open
stay where the gap began, so time is never invented across an outage and the totals run a
little low. The report is cached for five minutes and the page refreshes itself.

## Featured posts

Members nominate a post two ways, into one counter: a `⭐` reaction, or **Feature
this post** in a message's right-click Apps menu, for people who never discover
reactions. A member's vote counts once however they cast it, and removing the
star takes it back until the post is featured — after that it stays, because a
card that appears and vanishes reads as a moderator deleting somebody's post.
At `GREYBOT_FEATURE_THRESHOLD` nominations (default 4) greyBot posts a card to
`GREYBOT_FEATURED_CHANNEL_ID`: the author's name and avatar, the text, the first
image the post carried, a jump link, and the star count with the source channel.

A menu nomination is otherwise invisible — nobody else can see a post was put
forward — so greyBot puts the first `⭐` on the post itself. That reaction is both
the counter and the button: everyone else just clicks the pill. greyBot takes its
own star back as soon as a member has put one there, so the number on the pill is
the real vote count rather than greyBot's seed plus the votes. `feature_marks`
holds that one star per post: `pending` to place, `seeded` while greyBot's own is
the only one, `clearing` once a member has starred it, `done` after. Both the PUT
and the DELETE are idempotent, so a repeat costs nothing.

Only channels under `GREYBOT_FEATURE_CATEGORY_ID` are eligible, never the
featured channel itself and never greyBot's own posts. The reaction payload
carries no parent channel, so eligibility is checked again when the card is
built; a nomination somewhere ineligible simply settles `ineligible` and no card
appears. `feature_delivery` settles each post once and retries nothing —
`featured` is the complete result, `unknown` a write Discord never confirmed,
`gone` a deleted post. The menu command is registered from the Lambda's
`interactions.COMMANDS` but answered here, since this is what holds the counts.

## Saturday log routing

The Warcraft Logs integration posts every report its progression guild sees into the
progression log channel, so a progression raider who starts a report for the Saturday team
puts it in the wrong channel. With both `GREYBOT_PROG_LOGS_CHANNEL_ID` and
`GREYBOT_SAT_LOGS_CHANNEL_ID` set and `GREYBOT_ENFORCE=1`, `log_routing` moves those
reports. A post qualifies only when the integration's webhook wrote it, it carries a
`warcraftlogs.com/reports/` link, and its Eastern timestamp falls on a Saturday — or before
6am Sunday, for a raid that ran past midnight — and more than a day after a progression
night ended. Progression raids Tuesday and Thursday 9pm to midnight, and a report started
within 24 hours of one of those windows stays where it is, so a late Wednesday upload is
never treated as somebody else's raid.

The report is republished in the Saturday channel first and the original deleted second,
carrying the integration's own embed across with a footer saying where it came from.
`log_route_delivery` settles each message exactly once and retries nothing: `moved` is the
complete result, `duplicated` means the copy landed but the original is still there to
remove by hand, `unknown` means a write whose outcome Discord never confirmed, and `gone`
or `skipped` mean there was nothing to move. Members posting links themselves are untouched,
and nothing is moved out of the Saturday channel.

## Direct messages

With `GREYBOT_DM_OWNER_ID` set, the worker subscribes to direct messages and `dm_relay`
passes any member's DM to that one person's own DM with greyBot. The owner answers as
greyBot by using Discord's Reply on the forwarded message; greyBot delivers it and ticks
the owner's message, or crosses it and says so when the member's DMs are closed. Nothing is
resent automatically. A member is told once a day that a person reads these messages.
Message text is relayed and never stored: `dm_forwards` holds only which forwarded message
belongs to which member for 30 days, and the journal gets contentless `DM_FORWARDED` and
`DM_REPLIED` entries. The collector already ignores events outside the server, so DMs never
reach the message index. Unset, the intent is not requested and DMs are ignored as before.

## Raid roll call

When a raid team's first boss of the night dies, the Lambda posts an attendance card to that
team's bot channel, once per night: the raid as Warcraft Logs recorded it, grouped tank /
healer / damage, each row a Discord member, an arrow and the character they played. What
could not be paired is listed underneath as "In kill but not in Discord" and "In Discord but
not in kill". The members come from the team's voice channel at the second of the kill; the
card and the post say "Discord" and nothing more specific, and a test holds them to that.
The Lambda draws and posts it (`src/rollcall.py`, `src/rollcall_card.py`); this service
answers the one thing only it knows.

`POST /internal/roll-call` takes `{"channel_id", "at"}` and returns the human members the
journal places in that channel at that moment, with display names and picture links. It is
not a member route: the caller signs the body with `GREYBOT_ROLLCALL_SECRET[_SSM]`
(`X-Greybot-Signature: t=<unix>,v1=<hmac-sha256 of "t.body">`, five-minute window), and
without the secret the route is not registered at all. A stay cut by a fresh gateway session
or a host outage is not counted, so nobody is reported present through a real gap; a bare
disconnect that the gateway then resumes loses nothing and is ignored.

Each install's voice channel, card label and member-to-character map live in its DynamoDB
`ROLLCALL#SETUP` row, written by the `rollcall_setup` admin invoke with `live` off by default.
With `review` set to a Discord user id, a live card is held (`ROLLCALL#PENDING#<night>`) and
sent to that person's DMs with Post and Skip; only they can press either, the held payload is
what gets posted, and a night settles once.
`{"mode":"rollcall","team":…,"dry":true,"hours":72}` draws a past night under
`rollcall/preview/` without claiming or posting, which is how a mapping is checked before
`live` is set. Members nobody has mapped are matched to the kill's names by spelling.

## Run locally

Create a Python environment outside the checkout and install `requirements.txt`, then
`pip install --no-deps -r requirements-voice.txt`.
Run from `control/` so the existing `src/discord.py` cannot shadow `discord.py`.
Configuration comes from the process environment or SSM, never a tracked file:

| Variable | Meaning |
|---|---|
| `GREYBOT_GUILD_ID` | Single allowed server |
| `GREYBOT_CLIENT_ID` | Bot application/user ID, checked by the worker |
| `GREYBOT_BOT_TOKEN_SSM` | Existing protected bot-token parameter |
| `GREYBOT_OAUTH_SECRET_SSM` | Dashboard OAuth client-secret parameter |
| `GREYBOT_ORIGIN` | HTTPS origin; loopback HTTP allowed for local development |
| `GREYBOT_STATE_DIR` | Private directory outside the checkout; default under user local data |
| `GREYBOT_ARCHIVE_BUCKET` | Optional private locked archive |
| `GREYBOT_ARCHIVE_DIR` | Optional owner-recoverable archive on the same NAS |
| `GREYBOT_RETENTION_DAYS` | Explicit archive default, no assumed retention period |
| `GREYBOT_CAPTURE_CONTENT` | `1` enables local message text; default metadata only |
| `GREYBOT_PROG_LOGS_CHANNEL_ID` | Progression log channel the Warcraft Logs integration posts into |
| `GREYBOT_SAT_LOGS_CHANNEL_ID` | Saturday team's log channel; set with the one above or neither |
| `GREYBOT_FEATURED_CHANNEL_ID` | Where featured posts are published |
| `GREYBOT_FEATURE_CATEGORY_ID` | Category whose channels can be nominated from; set with the one above or neither |
| `GREYBOT_FEATURE_THRESHOLD` | Nominations needed to feature a post, minimum 2, default 4 |
| `GREYBOT_ENFORCE` | `1` enables requested moderation after archive checks |
| `AWS_PROFILE`, `AWS_DEFAULT_REGION` | AWS SDK authentication and region |

The two secret variables also accept their unsuffixed names as process values.
Never supply both sources. Register the exact `/auth/callback` URL in Discord's
OAuth settings. Login requests only `identify`; live bot REST checks validate
current membership and Administrator permission or guild ownership on every
private request. Session cookies contain random tokens; only hashes are stored.

```sh
python -m uvicorn greybot_control.web:create_app --factory --host 127.0.0.1 --port 8080 --no-access-log
python -m greybot_control.worker
python -m unittest discover -s tests -v
```

Production requires a supervised persistent host, persistent private storage,
TLS, and one Gateway worker per state directory. SQLite is single-host; do not
place it on NFS or run independent replicas. Do not expose the origin outside
the configured TLS proxy. Keep HTTP access logs disabled/redacted on the app
and proxy: OAuth callback query strings contain authorization codes. Gateway
membership and message-content intents require developer-portal enablement;
Discord permissions are a separate control. No voice audio is recorded.

The included Dockerfile builds from the repository root with
`docker build -f control/Dockerfile .`. It copies only the control source and
selected avatar, runs as a non-root user, and does not include raid credentials,
configuration, evidence, tests, or runtime databases. Start the same image with
`python -m greybot_control.worker` for the worker process, sharing the private
state volume on the same host. A NAS is optional: an always-on AWS host can run
the same processes behind HTTPS. Static hosting alone cannot run the Gateway.
Do not use separate hosts with this SQLite implementation; use a shared service
database before introducing multiple hosts or independent scaling.

## Audit guarantees and limits

The SQLite journal blocks application updates/deletes and chains event hashes.
It is **not immutable against the filesystem owner**, and an empty or truncated
journal can still have a valid chain. `infra/archive.cfn.json` prepares an
independent private S3 archive with explicit COMPLIANCE retention. Deployment
is separate; locked versions cannot be deleted or their retention shortened
during that period. S3 versioning alone is not that guarantee.

Archive writes are conditional, content-addressed and read back by version ID
before receipt recording. Original versions survive later writes to a key.
Grant the worker only PutObject, GetObject/GetObjectVersion, GetObjectRetention,
GetBucketObjectLockConfiguration and GetBucketVersioning on the archive/prefix;
no DeleteObject, DeleteObjectVersion, bypass, retention editing or bucket-policy
editing. Separate archive administration from dashboard administration. Use
independent version inventory/reconciliation to detect a damaged local index;
the local chain check alone does not certify archive completeness.

Pending local events are not yet locked. Moderation waits for the backlog to
archive before execution. A crash after a claim leaves `executing`, and an
ambiguous API result leaves `unknown`; neither is retried automatically. Recheck
Discord and record a separate corrective action. There is no distributed
transaction or blanket exactly-once promise. Archive outages stop dispatch.

Events retain identifiers and selected metadata. They exclude session material,
invite codes, attachments, raw audit reasons and message text. Optional message
text stays in the local search index. Captured edit/deletion versions are stored
separately with append-only database guards and a digest in the archived event;
the text itself is not copied into the metadata archive. The NAS owner retains
filesystem recovery and removal authority. Audit posts can show captured before/after
text, named added/removed roles, role and channel settings, and permission changes.
The collector does not invent old message content, moderation actors or missing
history; fresh Gateway sessions record a possible coverage gap. Metadata event
details unavailable in older records remain explicitly unavailable.

## Member verification and permission cutover

Configure `GREYBOT_DISCORD_PUBLIC_KEY`, `GREYBOT_TURNSTILE_SITE_KEY` and
`GREYBOT_TURNSTILE_SECRET` outside the checkout. The shared welcome button produces
an ephemeral response linking to `/verify`; the member signs in with Discord and
completes a server-validated Turnstile check. Member sessions cannot access admin
routes. The worker rechecks server screening, membership and role permissions.

The permission cutover is a separate explicit operation after verification works.
`python -m greybot_control.onboarding_gate --actor ADMIN_ID` previews a live plan;
`--apply REVIEW_REVISION` applies that exact reviewed state. Existing member access
must be preserved, every original permission is archived before changes, and the
global public view permission is removed last. A stale plan or unconfirmed write
stops the operation. Inspect the archived plan and step receipts before restoring
permissions after a partial cutover; do not blindly repeat a stale command.

Keep the previous verification and self-role providers until real replacement
interactions are validated. Keep any separately configured audit provider independent of this cutover.

## Reader transition

Install `requirements-reader.txt` for `python -m greybot_control.mcp_reader`.
This stdio server exposes search, channel reads, event history and index status.
It opens the index in SQLite read-only mode and needs only `GREYBOT_GUILD_ID`
and `GREYBOT_STATE_DIR`, with no bot token or OAuth secret. It is not a network
service and has no send/edit/delete tools. Client wiring remains a separate
cutover step; this is not yet a drop-in replacement for every legacy tool.

`python -m greybot_control.reader --query text` searches this service's local
guild index without an additional bot token. `--import-reader /absolute/path`
opens the prior SQLite index read-only, imports only the configured guild's
`source=bot` messages and leaves the source intact. Repeating skips existing
messages. Personal export and DM/local sources are deliberately not moved;
their existing local reader remains required until a reviewed bridge is added.
No live history import or client-registration change happens automatically.

## NAS deployment preparation

`compose.yaml` runs the web service and Gateway worker as a separate Compose
project, sharing only their private state directory. Set `GREYBOT_ENV_FILE` to
an absolute path outside the checkout containing the runtime variables above,
and `GREYBOT_DATA_DIR` to a private directory writable by container UID 10001.
Protect the environment file with mode 0600. Do not mount media shares or the
Docker socket. Both services restart automatically after a host reboot.

The web port binds only to NAS loopback at port 8088. An HTTPS reverse proxy
must be configured before remote use; a proxy in another container cannot use
its own loopback to reach this port. Choose an explicit shared Docker network
or verified host routing at deployment. Do not expose the plain HTTP port.
Compose construction and NAS startup still require validation on the host.

For a private NAS archive, include `compose.nas.yaml`, set `GREYBOT_AUDIT_DIR`
to its host directory and `GREYBOT_ARCHIVE_DIR=/archive` in the runtime
environment. Only the worker mounts that directory. `LocalArchive` writes
content-addressed event files without overwriting existing files, verifies
their content, and records receipts after durable writes. Configure either
the NAS directory or the S3 bucket, not both; either verified archive satisfies
the moderation gate. The web interface has no edit/delete archive operation.
The NAS owner retains direct restoration and removal access, so NAS files do
not provide S3 COMPLIANCE retention or owner-proof immutability.

## Retirement gates

The onboarding permission plan keeps the welcome channel and Discord's designated
server rules channel visible before verification. It uses `rules_channel_id`,
not matching channel names, so team-specific rules channels remain restricted.
It preserves existing write restrictions and reviews all existing members before
applying changes, with original permissions archived for NAS-owner recovery.

The `/poll` command is implemented in `src/polls.py`
through the existing signed Lambda interaction endpoint. It creates a native
Discord poll in the invoking channel with 2–10 answers, a duration dropdown
(1 hour through 7 days, default 24 hours), and optional multiple selections.
Both caller and app must have channel visibility, message and poll permissions;
administrators qualify. Discord manages voting and expiration. Native poll
controls use Discord's own rendering rather than custom embed styling.
No separate `/poll-end`, search, help, giveaway, X-feed or channel-guide
replacement is included. Command registration must run with the final Lambda
deployment, followed by live voting/expiry validation; offline tests alone do
not validate Discord rendering or delivery.

Before retirement, verify reader search coverage and client wiring, every raid
tenant's channel delivery using the retained application, scheduled jobs and
claims, moderation features and command overrides, preserved historical
state, admin access revocation, archive recovery, native safety coexistence and
rollback. Do not delete old apps/webhooks merely because the replacement starts.
Old messages/components remain owned by their original app or webhook.

References: [Discord OAuth](https://docs.discord.com/developers/topics/oauth2),
[Gateway](https://docs.discord.com/developers/events/gateway),
[permissions](https://docs.discord.com/developers/topics/permissions),
[S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html).
