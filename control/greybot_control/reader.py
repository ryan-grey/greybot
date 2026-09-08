"""Read-only bridge to the private guild index; no separate bot identity."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .config import Config
from .store import Store


def import_jsonl(store, source, guild):
    """Import a scoped export in batches; preserve newer live records."""
    count = 0
    batch = []
    def save():
        nonlocal count
        with store.connection() as db:
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO messages(id,guild,channel,author,content,deleted,observed) VALUES(?,?,?,?,?,?,?)", batch)
            count += db.total_changes - before
        batch.clear()
    with Path(source).open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["guild"] != guild:
                raise ValueError("Import contains a different guild")
            if not all(str(row[key]).isdecimal() for key in ("id", "channel")):
                raise ValueError("Invalid message or channel ID")
            batch.append((row["id"], guild, row["channel"], row["author"], row["content"], bool(row["deleted"]), float(row["observed"])))
            if len(batch) == 1000:
                save()
        if batch:
            save()
    return count


def import_guild(store, source, guild):
    source = Path(source).expanduser().resolve()
    if source == store.path.resolve():
        raise ValueError("Source and destination must differ")
    # Deliberately imports only this guild's bot-visible messages. Personal
    # export and local DM records remain in their original private store.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT id,channel_id,author_id,content,deleted FROM messages "
                          "WHERE source='bot' AND guild_id=? ORDER BY id", (guild,))
        count = 0
        for row in rows:
            if not store.message(guild, str(row["id"])):
                store.index_message(guild, str(row["id"]), str(row["channel_id"]),
                                    str(row["author_id"] or ""), row["content"] or "", bool(row["deleted"]))
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="")
    parser.add_argument("--channel", default="")
    parser.add_argument("--author", default="")
    parser.add_argument("--import-reader", type=Path)
    parser.add_argument("--import-jsonl", type=Path)
    args = parser.parse_args()
    cfg = Config.from_env()
    store = Store(cfg.state_dir / "control.sqlite3")
    if args.import_jsonl:
        print(json.dumps({"imported": import_jsonl(store, args.import_jsonl, cfg.guild_id)}))
    elif args.import_reader:
        print(json.dumps({"imported": import_guild(store, args.import_reader, cfg.guild_id)}))
    else:
        print(json.dumps(store.search(cfg.guild_id, args.query, args.channel, args.author)))


if __name__ == "__main__":
    main()
