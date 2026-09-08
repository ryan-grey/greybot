"""Import available Discord audit metadata without modifying source entries."""
import asyncio
import json
from urllib.parse import urlencode

from .config import Config
from .discord_api import DiscordAPI
from .store import Store
from .audit_details import project as audit_details


async def import_available(cfg, store, api, max_pages=100):
    before = ""
    imported = 0
    for _ in range(max_pages):
        query = {"limit": 100}
        if before:
            query["before"] = before
        result = await api.request("GET", f"/guilds/{cfg.guild_id}/audit-logs?" + urlencode(query))
        entries = result["audit_log_entries"]
        for entry in reversed(entries):
            eid = str(entry["id"])
            if store.audit_seen(cfg.guild_id, eid):
                continue
            payload = {"id": eid, "actor": entry.get("user_id"),
                       "target_id": entry.get("target_id"), "action_type": entry["action_type"],
                       "changed_fields": [c["key"] for c in entry.get("changes", [])],
                       "occurred_at": ((int(eid) >> 22) + 1420070400000) / 1000,
                       "source": "Discord audit history"}
            payload.update(audit_details(entry))
            # Arbitrary reasons, changed values and webhook/invite data excluded.
            store.append(f"discord-audit:{cfg.guild_id}:{eid}", cfg.guild_id,
                         "GUILD_AUDIT_LOG_ENTRY_CREATE", str(entry.get("target_id") or entry.get("user_id") or ""), payload)
            imported += 1
        if len(entries) < 100:
            return {"imported": imported, "available_history_exhausted": True}
        next_before = str(min(int(e["id"]) for e in entries))
        if before and int(next_before) >= int(before):
            raise RuntimeError("Audit history cursor did not advance")
        before = next_before
    return {"imported": imported, "available_history_exhausted": False, "before": before}


async def main():
    cfg = Config.from_env()
    store = Store(cfg.state_dir / "control.sqlite3")
    api = DiscordAPI(cfg)
    try:
        print(json.dumps(await import_available(cfg, store, api)))
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
