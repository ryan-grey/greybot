"""Reviewable permission plan exposing welcome and server rules before verification.

This module plans changes; it never changes Discord permissions on import or inspection.
Existing members without the verified role must be reviewed before applying a gate.
"""
from copy import deepcopy
import asyncio
import hashlib
import json
import secrets

from .discord_api import Denied, Unavailable
from .mutes import effective_permissions

VIEW = 1 << 10


def plan(guild, roles, channels, verified_role, welcome_channel, rules_channel=None):
    by_id = {role["id"]: role for role in roles}
    if verified_role not in by_id or verified_role == guild or guild not in by_id:
        raise Denied("Choose a valid verified member role")
    if welcome_channel not in {c["id"] for c in channels if c.get("type") in {0, 5}}:
        raise Denied("Choose a text welcome channel")
    if rules_channel and rules_channel not in {c["id"] for c in channels if c.get("type") in {0, 5}}:
        raise Denied("The server rules channel must be an available text channel")
    public_channels = {welcome_channel}
    public_channels.update(channel["id"] for channel in channels
                           if channel.get("type") in {0, 5}
                           and channel.get("name") in {"channel-preferences", "channel-list", "channel-guide", "verify-membership"})
    if rules_channel:
        public_channels.add(rules_channel)
    planned_roles = deepcopy(roles)
    changes = []
    baseline_view = bool((int(by_id[guild]["permissions"]) | int(by_id[verified_role]["permissions"])) & VIEW)
    for role in planned_roles:
        before = int(role["permissions"])
        after = before & ~VIEW if role["id"] == guild else before
        if role["id"] == verified_role and baseline_view:
            after |= VIEW
        if after != before:
            changes.append({"kind": "role_permissions", "role_id": role["id"], "before": str(before), "after": str(after)})
            role["permissions"] = str(after)
    anonymous = {"roles": [], "user": {"id": "0"}}
    verified = {"roles": [verified_role], "user": {"id": "0"}}
    planned_channels = []
    for channel in channels:
        # Thread visibility follows the parent; threads have no independent overwrites.
        if channel.get("type") in {10, 11, 12}:
            continue
        updated = deepcopy(channel)
        overwrites = updated.setdefault("permission_overwrites", [])
        before_verified = bool(effective_permissions(guild, roles, verified, channel) & VIEW)
        for target in (guild,):
            existing = next((o for o in overwrites if o["id"] == target and o["type"] == 0), None)
            old = deepcopy(existing)
            entry = existing if existing is not None else {"id": target, "type": 0, "allow": "0", "deny": "0"}
            # Remove explicit public visibility; keep all existing denies and role-specific rules.
            # A verified role carries the prior baseline visibility at guild level.
            if channel["id"] in public_channels:
                entry["allow"] = str(int(entry["allow"]) | VIEW)
                entry["deny"] = str(int(entry["deny"]) & ~VIEW)
            else:
                entry["allow"] = str(int(entry["allow"]) & ~VIEW)
                if existing is None:
                    continue
            if old != entry:
                if existing is None:
                    overwrites.append(entry)
                changes.append({"kind": "channel_overwrite", "channel_id": channel["id"], "role_id": target,
                                "before": old, "after": deepcopy(entry)})
        if len(overwrites) > 100:
            raise Denied("A channel lacks space for the required permission overwrites")
        after_new = bool(effective_permissions(guild, planned_roles, anonymous, updated) & VIEW)
        after_verified = bool(effective_permissions(guild, planned_roles, verified, updated) & VIEW)
        if after_new != (channel["id"] in public_channels) or after_verified != before_verified:
            raise Denied("The permission plan does not preserve the required access")
        planned_channels.append(updated)
    return {"changes": changes, "roles": planned_roles, "channels": planned_channels}


def affected_existing_members(members, verified_role, owner_id):
    return [m["user"]["id"] for m in members if not m["user"].get("bot")
            and m["user"]["id"] != owner_id and verified_role not in m.get("roles", [])]


def review_members(guild, roles, channels, members, owner_id, verified_role, planned):
    """Find existing access changes before allowing a permission cutover.

    Propose a starter role only when it restores visibility without granting any
    new effective permissions. Pending screening members are never grandfathered.
    The caller must resolve every blocked member before applying the plan.
    """
    original = {c["id"]: c for c in channels}
    additions, blocked = [], []
    for member in members:
        user = member["user"]["id"]
        if user == owner_id:
            continue

        def differences(candidate):
            lost, gained = [], []
            for channel in planned["channels"]:
                before = effective_permissions(guild, roles, member, original[channel["id"]])
                after = effective_permissions(guild, planned["roles"], candidate, channel)
                # Permissions in invisible channels cannot be exercised.
                before = before if before & VIEW else 0
                after = after if after & VIEW else 0
                if before & ~after:
                    lost.append(channel["id"])
                if after & ~before:
                    gained.append(channel["id"])
            return lost, gained

        lost, gained = differences(member)
        if not lost and not gained:
            continue
        candidate = deepcopy(member)
        candidate["roles"] = list(set(member.get("roles", [])) | {verified_role})
        repaired = differences(candidate)
        if (lost and not gained and not any(repaired) and not member.get("pending")
                and not member["user"].get("bot") and verified_role not in member.get("roles", [])):
            additions.append(user)
        else:
            blocked.append({"user_id": user, "lost_channels": lost, "gained_channels": gained})
    return {"starter_role_additions": additions, "blocked": blocked}


async def snapshot(cfg, store, api):
    """Read all relevant live state; this does not change server permissions."""
    from .role_panels import validate
    values = store.settings(cfg.guild_id)["values"]
    role, welcome = values.get("verification_role", ""), values.get("welcome_channel", "")
    await validate(cfg, api, [role])
    guild = await api.request("GET", f"/guilds/{cfg.guild_id}")
    roles = await api.request("GET", f"/guilds/{cfg.guild_id}/roles")
    channels = await api.request("GET", f"/guilds/{cfg.guild_id}/channels")
    members, after = [], "0"
    while True:
        page = await api.request("GET", f"/guilds/{cfg.guild_id}/members?limit=1000&after={after}")
        members.extend(page)
        if len(page) < 1000:
            break
        next_after = str(max(int(m["user"]["id"]) for m in page))
        if int(next_after) <= int(after):
            raise Unavailable("Member pagination did not advance")
        after = next_after
    rules_channel = guild.get("rules_channel_id")
    planned = plan(cfg.guild_id, roles, channels, role, welcome, rules_channel)
    review = review_members(cfg.guild_id, roles, channels, members, guild["owner_id"], role, planned)
    # Only permission-related state enters the review digest. Names and activity
    # changes do not invalidate a plan, but membership and permission changes do.
    baseline = {
        "owner": guild["owner_id"], "role": role, "welcome": welcome, "rules_channel": rules_channel,
        "roles": sorted((r["id"], str(r["permissions"]), r["position"]) for r in roles),
        "channels": sorted((c["id"], c["type"], c.get("parent_id") or "",
            sorted((o["id"], o["type"], str(o["allow"]), str(o["deny"])) for o in c.get("permission_overwrites", []))) for c in channels),
        "members": sorted((m["user"]["id"], sorted(m.get("roles", [])), bool(m.get("pending")),
                           m.get("joined_at"), bool(m["user"].get("bot"))) for m in members),
    }
    revision = hashlib.sha256(json.dumps(baseline, sort_keys=True).encode()).hexdigest()
    return {"revision": revision, "changes": planned["changes"], "review": review,
            "member_count": len(members), "roles": roles, "channels": channels}


async def apply(cfg, store, api, archive, actor, revision):
    """Apply an explicitly reviewed cutover, archiving every before/after value.

    No automatic retry follows an ambiguous Discord write. The archived plan and
    per-step receipts let the NAS owner inspect or restore a partial cutover.
    """
    import os
    from .verification import configured
    await api.require_admin(actor)
    values = store.settings(cfg.guild_id)["values"]
    if (not cfg.enforce or not archive or not configured()
            or not os.environ.get("GREYBOT_DISCORD_PUBLIC_KEY")
            or not values.get("verification_enabled") or not values.get("welcome_enabled")):
        raise Denied("Enable and validate member verification before applying the gate")
    current = await snapshot(cfg, store, api)
    if current["revision"] != revision:
        raise Denied("Server permissions or membership changed; review a new plan")
    if current["review"]["blocked"] or current["review"]["starter_role_additions"]:
        raise Denied("Resolve existing member access changes before applying the gate")
    run = secrets.token_hex(16)
    channels = {c["id"]: c for c in current["channels"]}
    def order(change):
        if change["kind"] == "role_permissions":
            return 3 if change["role_id"] == cfg.guild_id else 0
        return 2 if channels[change["channel_id"]]["type"] == 4 else 1
    changes = sorted(current["changes"], key=order)

    async def record(suffix, kind, payload):
        event = store.append("onboarding:" + run + ":" + suffix, cfg.guild_id, kind, "", {"actor": actor, **payload})
        def outstanding():
            with store.connection() as db:
                return db.execute("SELECT COUNT(*) FROM events e LEFT JOIN receipts r ON e.seq=r.seq "
                                  "WHERE e.seq<=? AND r.seq IS NULL", (event["seq"],)).fetchone()[0]
        # The collector keeps appending while the archive flushes. Require a
        # durable prefix through this record, not an empty global live queue.
        remaining = outstanding()
        while remaining:
            await asyncio.to_thread(archive.flush, store)
            after = outstanding()
            if after >= remaining:
                raise Unavailable("Waiting for onboarding audit archive")
            remaining = after

    await record("plan", "ONBOARDING_GATE_PLANNED", {"revision": revision, "changes": changes})
    for index, change in enumerate(changes):
        # Recheck the administrator and the exact target immediately before writing.
        await api.require_admin(actor)
        if store.settings(cfg.guild_id)["values"] != values:
            raise Denied("Verification settings changed during cutover")
        role = change["role_id"]
        if change["kind"] == "role_permissions":
            live = await api.request("GET", f"/guilds/{cfg.guild_id}/roles")
            before = next((str(r["permissions"]) for r in live if r["id"] == role), None)
            if before != change["before"]:
                raise Denied("Role permissions changed during cutover")
            path = f"/guilds/{cfg.guild_id}/roles/{role}"
            await api.request("PATCH", path, body={"permissions": change["after"]}, reason="greyBot verified-member gate")
            live = await api.request("GET", f"/guilds/{cfg.guild_id}/roles")
            after = next((str(r["permissions"]) for r in live if r["id"] == role), None)
        else:
            channel = change["channel_id"]
            def entry(live):
                found = next((o for o in live.get("permission_overwrites", []) if o["id"] == role and o["type"] == 0), None)
                return {k: found[k] for k in ("id", "type", "allow", "deny")} if found else None
            live = await api.request("GET", f"/channels/{channel}")
            before = entry(live)
            # A category edit may already have updated a synchronized child.
            if before == change["after"]:
                await record(str(index), "ONBOARDING_GATE_STEP_VERIFIED", change)
                continue
            if before != change["before"]:
                raise Denied("Channel permissions changed during cutover")
            await api.request("PUT", f"/channels/{channel}/permissions/{role}",
                body={k: change["after"][k] for k in ("type", "allow", "deny")}, reason="greyBot verified-member gate")
            after = entry(await api.request("GET", f"/channels/{channel}"))
        if after != change["after"]:
            raise Unavailable("Permission write was not confirmed; inspect the archived cutover plan")
        await record(str(index), "ONBOARDING_GATE_STEP_VERIFIED", change)
    verified = await snapshot(cfg, store, api)
    if verified["changes"]:
        raise Unavailable("Gate changed during cutover; inspect the archived plan")
    await record("complete", "ONBOARDING_GATE_APPLIED", {"changes": len(changes)})
    return {"applied": len(changes), "run": run}


async def main():
    import argparse
    from .config import Config
    from .discord_api import DiscordAPI
    from .store import Store
    from .archive import Archive
    from .local_archive import LocalArchive
    parser = argparse.ArgumentParser(description="Preview or apply the verified-member permission gate")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--apply", metavar="REVIEW_REVISION")
    args = parser.parse_args()
    cfg = Config.from_env()
    store = Store(cfg.state_dir / "control.sqlite3")
    api = DiscordAPI(cfg)
    try:
        await api.require_admin(args.actor)
        if args.apply:
            if cfg.archive_dir:
                archive = LocalArchive(cfg.archive_dir)
            elif cfg.archive_bucket:
                import boto3
                archive = Archive(boto3.client("s3"), cfg.archive_bucket, cfg.retention_days)
            else:
                raise Denied("Configure an audit archive before applying the gate")
            result = await apply(cfg, store, api, archive, args.actor, args.apply)
        else:
            result = await snapshot(cfg, store, api)
            result.pop("roles"); result.pop("channels")
        print(json.dumps(result))
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
