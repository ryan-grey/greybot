"""Tests for ingest.py and the asar writer. Everything is synthetic; runs
under pytest or plainly with python3 tests/test_ingest.py."""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import ingest  # noqa: E402
import patch_discord  # noqa: E402

CH = {"id": "700000000000000101", "guild_id": None, "guild_name": None,
      "name": "alexfixture", "type": 1, "recipients": ["900000000000000002"]}
GUILD_CH = {"id": "700000000000000001", "guild_id": "800000000000000001",
            "guild_name": "Fixture Guild", "name": "general", "type": 0, "recipients": []}


def ev_message(op, mid, ch, content, author="Alex Fixture", ts="2026-08-01T10:00:00.000Z", **extra):
    m = {"id": str(mid), "channel_id": ch["id"], "author_id": "900000000000000002",
         "author_name": author, "content": content, "ts": ts, "edited_ts": None,
         "attachments": []}
    m.update(extra)
    return json.dumps({"kind": "message", "op": op, "channel": ch, "message": m,
                       "at": ts})


def fresh():
    d = Path(tempfile.mkdtemp(prefix="discord-local-log-test-"))
    return d / "events.jsonl", d / "discord.db"


def rows(db, sql, *args):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def test_create_backfill_update_delete():
    events, db = fresh()
    lines = [
        ev_message("create", 1001, CH, "pizza on friday?"),
        ev_message("backfill", 1000, CH, "earlier message", ts="2026-07-31T09:00:00Z"),
        ev_message("create", 2001, GUILD_CH, "raid at 8", author="Bwhip"),
        # partial update: only content + edited_ts
        json.dumps({"kind": "message", "op": "update", "channel": {"id": CH["id"]},
                    "message": {"id": "1001", "channel_id": CH["id"], "content": "pizza on saturday?",
                                "edited_ts": "2026-08-01T10:30:00Z", "attachments": []}}),
        json.dumps({"kind": "delete", "id": "1000", "channel_id": CH["id"]}),
        json.dumps({"kind": "delete", "ids": ["2001", "2002"], "channel_id": GUILD_CH["id"]}),
        "not json at all",
    ]
    events.write_text("\n".join(lines) + "\n")
    out = ingest.ingest(events, db)
    assert out["applied"] == {"message": 3, "update": 1, "delete": 3, "bad": 1}
    m = {r["id"]: r for r in rows(db, "SELECT * FROM messages")}
    assert m[1001]["content"] == "pizza on saturday?"
    assert m[1001]["edited_ts"] == 1785580200000
    assert m[1001]["author_name"] == "Alex Fixture"  # untouched by the partial update
    assert m[1000]["deleted"] == 1 and m[1000]["content"] == "earlier message"
    assert m[2001]["deleted"] == 1 and m[2001]["guild_id"] == "800000000000000001"
    assert m[2002]["deleted"] == 1 and m[2002]["channel_id"] == GUILD_CH["id"]  # tombstone
    assert m[2002]["ts"] == ingest._snowflake_ms(2002)
    revs = sorted(r["rev"] for r in m.values())
    assert revs == sorted(set(revs)) and max(revs) == out["rev"]
    ch = {r["id"]: r for r in rows(db, "SELECT * FROM channels")}
    assert ch[CH["id"]]["type"] == 1 and json.loads(ch[CH["id"]]["recipients"]) == CH["recipients"]
    assert ch[GUILD_CH["id"]]["guild_name"] == "Fixture Guild"


def test_offset_resume_and_partial_line():
    events, db = fresh()
    events.write_text(ev_message("create", 1, CH, "one") + "\n")
    assert ingest.ingest(events, db)["applied"]["message"] == 1
    # a second pass with nothing new applies nothing
    assert ingest.ingest(events, db)["applied"] == {"message": 0, "update": 0, "delete": 0, "bad": 0}
    # append a full line plus a half-written one
    with open(events, "a") as f:
        f.write(ev_message("create", 2, CH, "two") + "\n")
        f.write('{"kind":"message","op":"create","chan')
    out = ingest.ingest(events, db)
    assert out["applied"]["message"] == 1 and out["applied"]["bad"] == 0
    with open(events, "a") as f:  # the writer finishes its line
        f.write('nel":' + json.dumps(CH) + ',"message":' + json.dumps(
            {"id": "3", "channel_id": CH["id"], "content": "three"}) + "}\n")
    assert ingest.ingest(events, db)["applied"]["message"] == 1
    assert [r["id"] for r in rows(db, "SELECT id FROM messages ORDER BY id")] == [1, 2, 3]


def test_rotation_drains_late_lines():
    events, db = fresh()
    events.write_text(ev_message("create", 1, CH, "x" * 100) + "\n")
    out = ingest.ingest(events, db, rotate_at=10)  # tiny threshold forces a rotate
    assert out["applied"]["message"] == 1
    assert not events.exists()
    assert not list(events.parent.glob("events.*.jsonl"))
    # the next write starts a fresh file at offset 0
    events.write_text(ev_message("create", 2, CH, "after rotate") + "\n")
    assert ingest.ingest(events, db)["applied"]["message"] == 1
    assert len(rows(db, "SELECT id FROM messages")) == 2


def test_update_before_create_inserts():
    events, db = fresh()
    events.write_text(json.dumps({
        "kind": "message", "op": "update", "channel": {"id": CH["id"]},
        "message": {"id": "77", "channel_id": CH["id"], "content": "edited first"}}) + "\n")
    out = ingest.ingest(events, db)
    assert out["applied"]["message"] == 1 and out["applied"]["update"] == 0
    assert rows(db, "SELECT content FROM messages")[0]["content"] == "edited first"


def test_asar_roundtrip():
    files = {"index.js": b'require("/x/dist/patcher.js")', "package.json": patch_discord.PACKAGE_JSON.encode()}
    blob = patch_discord.asar_bytes(files)
    assert patch_discord.parse_asar(blob) == files
    # header words: pickle size 4, then aligned+8, aligned+4, raw header len
    import struct
    a, b, c, d = struct.unpack("<IIII", blob[:16])
    assert a == 4 and b == c + 4 and c - 4 == (d + 3) & ~3


def test_patch_and_unpatch_on_fake_bundle():
    res = Path(tempfile.mkdtemp(prefix="fake-discord-")) / "Resources"
    res.mkdir()
    (res / "app.asar").write_bytes(b"ORIGINAL")
    build = res.parent / "Vencord"
    (build / "dist").mkdir(parents=True)
    assert "error" in patch_discord.patch(build, res)  # no patcher.js yet
    (build / "dist" / "patcher.js").write_text("// built")
    out = patch_discord.patch(build, res)
    assert out["patched"] and "patcher.js" in out["index_js"]
    assert (res / "_app.asar").read_bytes() == b"ORIGINAL"
    assert patch_discord.status(res)["index_js"] == out["index_js"]
    # re-running is idempotent and never loses the original
    patch_discord.patch(build, res)
    assert (res / "_app.asar").read_bytes() == b"ORIGINAL"
    assert patch_discord.unpatch(res) == {"patched": False}
    assert (res / "app.asar").read_bytes() == b"ORIGINAL"
    assert not (res / "_app.asar").exists()
    assert patch_discord.status(res) == {"patched": False}


if __name__ == "__main__":
    names = [n for n in dir() if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok", n)
    print(f"{len(names)} passed")

