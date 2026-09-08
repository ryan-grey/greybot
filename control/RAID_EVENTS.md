# Raid events

The NAS hosts raid rosters alongside the existing control service. Event data,
signup preferences and imported history stay in its private SQLite database;
the public repository contains no server exports or member fixtures.

## Entry points

- `/create`: guided Discord form; channel and signup template are optional command choices.
- `/quickcreate`: title and an ISO date with UTC offset, plus optional channel,
  template and details. Example time: `2026-09-12T18:00-07:00`.
- `/raid`: opens the authenticated `/raids` page.
- Dashboard → Raid events opens the same member-authenticated page. The rest of
  the administration site retains its separate Administrator requirement.
- Card controls support signup/spec changes, Bench/Late/Tentative/Absence,
  notes and withdrawal. Web controls also provide event editing and closing/reopening.
- Each event has an authenticated calendar download with UTC times; roster
  channel access is checked again before the download is returned.

Creation requires Administrator or Manage Server. Event leaders, co-leaders,
Administrator and Manage Server can manage an event. Every queued action
refreshes membership and channel permissions before application; signups also
check the event's allowed-role list and verification gate. No signup assigns a
Discord role. Default command permissions are a convenience, not the security boundary.

## Persistence and delivery

`raids.py` applies roster updates in SQLite transactions with revision checks
and an append-only journal entry. Replayed request IDs do not repeat mutations.
The worker archives the journal before publishing or updating a card. One
member occupies at most one roster entry; notes and entry times survive changes.
Capacity checks use template class types, not the imported `status` field.
The worker closes expired events and updates their controls.

New-post failures with uncertain delivery stop automatic retries to avoid
duplicate announcements. Existing-message updates can retry safely. Inspect
the event's delivery state and Discord before reconciling a failed initial post.
There is no automatic destructive rollback of event or journal data.

## Migration and activation

1. Preserve server configuration, Discord role permissions, channel overrides,
   command restrictions, event IDs and a complete private event export.
2. Validate the export against a private file containing one expected event ID
   per line using `python -m greybot_control.raid_import DIRECTORY --guild GUILD
   --expected-ids IDS_FILE`. No database is changed without `--database`.
3. Deploy the candidate with `GREYBOT_RAIDS_ENABLED=0`. Import historical
   snapshots with `python -m greybot_control.raid_migrate DIRECTORY
   --expected-ids IDS_FILE` in the configured private runtime.
4. Validate authentication, old workflows, history rendering and journal integrity.
   Register the commands returned by `raid_discord.commands()` while preserving
   the existing command set, then enable the raid switch in web and worker.
5. Create a clearly labelled test event in an appropriate private channel and
   validate actual Discord interactions, worker results, card updates and restart.
6. Refresh the old live roster immediately before cutover. Prepare only reviewed
   current events with `--activate-event SOURCE_ID --actor ADMIN_ID`; the command
   preserves original leaders, roster, template, settings and allowed-role list.
   A repeated activation does not overwrite a new roster with old source data.
7. Confirm the replacement and every signup, then retire the old integration.
   Preserve historical Discord messages and the private exports.

The history scoreboard counts recorded signups, not verified raid attendance.
Imported events can contain past class/spec definitions; their original JSON is
retained without lossy normalization. Runtime activation is explicit, so importing
old history cannot flood channels with historical cards.
