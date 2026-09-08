# One greyBot source and release

The `greybot` repository is the maintained source for the Discord application,
raid announcements, signup service, admin website, guild history collector,
local MCP reader and optional local capture integration.

| Component | Source | Runtime |
| --- | --- | --- |
| Raid announcements, scorecards and signed commands | `src/`, `cdk/` | AWS Lambda and existing AWS storage |
| Admin site, moderation, verification and signups | `control/` | Persistent Docker host |
| Personal/local MCP reader | `integrations/discord-mcp/` | Local computer only |
| Optional desktop capture and ingestion | `integrations/discord-local-log/` | Local computer only |

Run `python3 scripts/build-release.py` for one release directory containing the
Lambda package, Docker build context and local integration sources, with a SHA-256
manifest. This unifies source and release packaging; it does not move AWS schedules
or personal desktop capture into the NAS container. Deploy the Lambda using
`scripts/deploy-cdk.sh prod` and the control service using its Compose configuration.
Install desktop integrations only on the owner's computer.

The former standalone `discord-mcp` and `discord-local-log` repositories are
superseded by these directories. Their original licenses remain beside their code;
the capture integration retains its GPL license and the MCP reader its MIT license.
Existing local database paths remain compatible. No private database, export,
message history, credential, runtime environment or archive belongs in this release.
The hosted admin site remains guild-scoped and does not ingest personal DMs.

The legacy announcement webhook is retired in favor of the retained Discord bot
application. Historical webhook messages remain in Discord. Raid claims, schedules,
tenant destinations and scorecard storage must be preserved during deployment.
