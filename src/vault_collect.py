"""Hourly, non-posting collector for current and previous weekly vault evidence."""
from datetime import datetime, timedelta, timezone

import blizzard
import config
import health
import raiderio
import store
import vault
import vault_history as history
import vault_leaderboards as leaderboards
import vault_sources as sources
import wcl


def collect(cfg, now, *, dry=False):
    from handler import tenant_configs, VAULT_TEAM
    pairs = [(s, c) for s, c in tenant_configs(cfg) if s.team == VAULT_TEAM]
    if not pairs:
        raise RuntimeError("no prog raid install configured")
    scope, team = pairs[0]
    setup = store.get_rollcall_setup(scope) or {}
    mapping = vault.character_mapping(setup.get("members") or {}, store.get_vault_characters(scope))
    guild = team.get("discord_guild_id") or cfg["discord_guild_id"]
    def discord_get(path):
        status, body = health._get("https://discord.com/api/v10" + path, token=cfg["bot_token"])
        if status != 200:
            raise RuntimeError("vault collector cannot read prog roster")
        return body
    members = vault.fetch_members(discord_get, guild, team.get("role_id") or cfg["role_id"])
    # Resolve realms from the actual roster, never guess a default realm for an unknown
    # character. If the roster fails, the invocation fails and stored history remains.
    roster_doc = raiderio._get("https://raider.io/api/v1/guilds/profile", {
        "region": cfg["guild_region"], "realm": cfg["guild_realm"],
        "name": cfg["guild_name"], "fields": "members"})
    roster = {vault.fold(m["character"]["name"]): m["character"] for m in roster_doc.get("members") or []}
    characters, skipped = [], 0
    for member in members:
        names = mapping.get(member["user"]["id"], [])
        if len(names) == 1 and (roster.get(vault.fold(names[0])) or {}).get("realm"):
            characters.extend(names)
        else:
            skipped += 1
    if not characters:
        raise RuntimeError("no unambiguous prog character identities available")
    profiles = vault.fetch_profiles(characters, roster, {}, cfg["guild_region"], cfg["guild_realm"])
    start, reset = vault.week_window(now)
    end = reset + timedelta(days=7)
    def save(bkeys, wkeys):
        return {hi.date().isoformat(): history.for_profiles(
                    scope, profiles, cfg["guild_region"], lo, hi, bkeys, wkeys, persist=not dry)
                for lo, hi in ((start, reset), (reset, end))}
    # Checkpoint each provider so a later slow/offline provider cannot discard runs
    # already observed in this invocation. Every write merges with the existing week.
    save({}, {})
    bkeys, boards, wkeys = {}, {}, {}
    try:
        token = blizzard.get_token(cfg["blizzard_client_id"], cfg["blizzard_client_secret"])
        bkeys = sources.fetch_blizzard(profiles, token, blizzard._get, start, end, records=True)
        save(bkeys, {})
        boards = leaderboards.fetch(profiles, token, blizzard._get, start, end)
    except Exception as exc:
        vault.log("vault_collector_blizzard_unavailable", error=type(exc).__name__)
    combined = {key: ((bkeys.get(key) or []) + (boards.get(key) or [])
                      if bkeys.get(key) is not None or boards.get(key) is not None else None)
                for key in profiles}
    save(combined, {})
    try:
        token = wcl.get_token(cfg["wcl_client_id"], cfg["wcl_client_secret"],
                              user_auth=cfg.get("wcl_user_auth"))
        wkeys = sources.fetch_wcl(profiles, token, start, end, region=cfg["guild_region"], records=True)
    except Exception as exc:
        vault.log("vault_collector_wcl_unavailable", error=type(exc).__name__)
    observed = {source: sum(1 for k, p in profiles.items() if
                (not p.get("rio_unavailable") if source == "Raider.IO" else
                 (combined if source == "Blizzard" else wkeys).get(k) is not None))
                for source in sources.SOURCES}
    if not any(observed.values()):
        raise RuntimeError("all vault providers unavailable; existing history retained")
    weeks = save(combined, wkeys)
    result = {"ok": True, "dry": dry, "characters": len(profiles), "skipped": skipped,
              "provider_characters": observed,
              "leaderboard_characters": sum(v is not None for v in boards.values()),
              "leaderboard_runs": sum(len(v or []) for v in boards.values()),
              "week_ends": list(weeks)}
    if not dry:
        import os, json
        table = os.environ["VAULT_HISTORY_TABLE"]
        store.ddb.put_item(TableName=table, Item={
            "pk": {"S": scope.tenant}, "sk": {"S": "COLLECTOR"},
            "summary": {"S": json.dumps(result)}, "updatedAt": {"S": now.isoformat()},
            "expiresAt": {"N": str(int((now + timedelta(days=history.RETENTION_DAYS)).timestamp()))}})
    vault.log("vault_collected", **result)
    return result


def handler(event, context):
    return collect(config.load(), datetime.now(timezone.utc), dry=bool((event or {}).get("dry")))
