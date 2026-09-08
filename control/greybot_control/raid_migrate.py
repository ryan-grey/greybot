"""Import private event history and explicitly prepare selected live replacements."""
import argparse
import copy
import json
from pathlib import Path
import time

from .config import Config
from .raid_import import load_export, import_events
from . import raids
from .store import Store


def prepare(store, guild, events, activate_ids, actor):
    selected = {str(e["id"]): e for e in events}
    if set(activate_ids) - selected.keys():
        raise ValueError("An activation ID is missing from the verified export")
    for source_id in activate_ids:
        e = selected[source_id]
        if e["closingTime"] <= time.time():
            raise ValueError("Cannot activate an event whose signup deadline has passed")
        raids.validate_event({**e, "state": "open"})
    result = import_events(store, guild, events)
    prepared = []
    for source_id in activate_ids:
        event = copy.deepcopy(selected[source_id])
        event["source_event_id"] = source_id
        event["state"] = "open"
        # Exact existing roster, co-leaders, settings and allowed roles survive.
        raid_id = raids.create(store, guild, actor, "migration:" + source_id, event)
        prepared.append({"source_event_id": source_id, "raid_id": raid_id})
    return {**result, "prepared": prepared}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected-ids", type=Path, required=True)
    parser.add_argument("--activate-event", action="append", default=[])
    parser.add_argument("--actor", help="Administrator performing the reviewed migration")
    args = parser.parse_args()
    cfg = Config.from_env()
    if args.activate_event and (not args.actor or not args.actor.isdecimal()):
        parser.error("Activation requires an explicit administrator actor")
    store = Store(cfg.state_dir / "control.sqlite3")
    raids.install(store)
    if not store.verify():
        raise ValueError("Journal integrity check failed")
    events = load_export(args.directory, cfg.guild_id, args.expected_ids.read_text().split())
    print(json.dumps(prepare(store, cfg.guild_id, events, args.activate_event, args.actor or cfg.client_id)))


if __name__ == "__main__":
    main()
