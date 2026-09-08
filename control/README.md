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

## Run locally

Create a Python environment outside the checkout and install `requirements.txt`.
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
