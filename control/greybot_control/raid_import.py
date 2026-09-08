"""Lossless private event migration, without publishing messages or changing roles.

Imports are versioned by content digest. A changed source remains a new snapshot,
so refreshing the active roster before cutover never destroys earlier evidence.
"""
import argparse
import json
from pathlib import Path

from .store import Store, canonical, digest


def validate(event, guild):
    if not isinstance(event, dict) or str(event.get("serverId")) != guild:
        raise ValueError("Event belongs to a different server")
    for key in ("id", "channelId", "leaderId"):
        if not isinstance(event.get(key), (str, int)) or not str(event[key]).isdecimal():
            raise ValueError("Event has an invalid " + key)
    if not isinstance(event.get("signUps"), list):
        raise ValueError("Event roster is missing")
    for signup in event["signUps"]:
        if not isinstance(signup, dict) or not str(signup.get("userId", "")).isdecimal():
            raise ValueError("Roster contains an invalid member")
    if not isinstance(event.get("classes"), list) or not isinstance(event.get("advancedSettings"), dict):
        raise ValueError("Event template or settings are missing")
    for key in ("startTime", "closingTime"):
        if isinstance(event.get(key), bool) or not isinstance(event.get(key), (int, float)):
            raise ValueError("Event has an invalid " + key)
    return event


def load_export(directory, guild, expected_ids=None):
    """Validate the entire export before writing anything to the journal."""
    events = []
    seen = set()
    for path in sorted(Path(directory).glob("*.json")):
        event = validate(json.loads(path.read_text()), guild)
        source_id = str(event["id"])
        if source_id in seen:
            raise ValueError("Export contains a duplicate event")
        if path.stem != source_id:
            raise ValueError("Export filename does not match its event")
        seen.add(source_id)
        events.append(event)
    if not events:
        raise ValueError("Export is empty")
    if expected_ids is not None and seen != set(map(str, expected_ids)):
        raise ValueError("Export does not match the expected event inventory")
    return events


def import_events(store, guild, events):
    """Atomically archive snapshots; no runtime signup table or live posts changed."""
    events = [validate(event, guild) for event in events]
    imported = unchanged = 0
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for event in events:
            body = canonical(event)
            checksum = digest(body)
            event_id = f"raid-import:{guild}:{event['id']}:{checksum}"
            prior = db.execute("SELECT payload FROM events WHERE event_id=?", (event_id,)).fetchone()
            if prior:
                detail = db.execute("SELECT body FROM event_details WHERE guild=? AND event_id=?",
                                    (guild, event_id)).fetchone()
                if not detail or digest(detail[0]) != checksum:
                    raise ValueError("Previously imported event failed its integrity check")
                unchanged += 1
                continue
            db.execute("INSERT INTO event_details VALUES(?,?,?)", (event_id, guild, body))
            store._append(db, event_id, guild, "RAID_HISTORY_IMPORTED", "", {
                "source": "raid-helper", "source_event_id": str(event["id"]),
                "channel_id": str(event["channelId"]), "title": event.get("title", ""),
                "start_time": event["startTime"], "signup_count": len(event["signUps"]),
                "detail_digest": checksum,
            })
            imported += 1
    return {"imported": imported, "unchanged": unchanged,
            "events": len(events), "signups": sum(len(e["signUps"]) for e in events)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--guild", required=True)
    parser.add_argument("--expected-ids", type=Path, required=True,
                        help="Private file containing one event ID per line")
    parser.add_argument("--database", type=Path,
                        help="Private database to import into; omit for validation only")
    args = parser.parse_args()
    events = load_export(args.directory, args.guild, args.expected_ids.read_text().split())
    if args.database:
        checkout = Path(__file__).resolve().parents[2]
        if args.database.resolve().is_relative_to(checkout):
            parser.error("The private database must be outside the repository")
        result = import_events(Store(args.database), args.guild, events)
    else:
        result = {"validated": len(events), "signups": sum(len(e["signUps"]) for e in events)}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
