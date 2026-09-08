"""Tests against a synthetic export ZIP and a fake REST API. No network,
no real Discord data. Runs under pytest or plainly:

    python3 tests/test_server.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

import make_fixture as fx  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="discord-mcp-test-")
os.environ["DISCORD_MCP_STATE_DIR"] = _TMP
os.environ["DISCORD_INDEX_DB"] = os.path.join(_TMP, "index.db")
os.environ["DISCORD_CHANGES_LOG"] = os.path.join(_TMP, "discord-changes.log")
os.environ["DISCORD_LOCAL_DB"] = os.path.join(_TMP, "no-local.db")
os.environ["DISCORD_BOT_TOKEN"] = "fixture-token-never-real"

import server  # noqa: E402  (env must be set before this import)

EXPORT_ZIP = os.path.join(_TMP, "package.zip")
fx.build_export_zip(EXPORT_ZIP)
FAKE = fx.FakeDiscord()
server._api = FAKE


def _j(s):
    return json.loads(s)


def setup_module(_=None):
    if os.path.exists(os.environ["DISCORD_INDEX_DB"]):
        os.remove(os.environ["DISCORD_INDEX_DB"])
    FAKE.__init__()
    server._api = FAKE


def test_snowflake_time():
    mid = fx.snowflake("2026-08-01T10:00:00+00:00")
    assert server._snowflake_ms(mid) == server._ms("2026-08-01T10:00:00+00:00")
    assert server._ms("2026-08-01 10:00:00.123000+00:00") == 1785578400123


def test_import_export_counts_and_idempotency():
    out = _j(server.import_export(EXPORT_ZIP))
    assert out["account"] == "Fixture Me"
    assert out["inserted"] == 5 and out["kept"] == 0
    assert out["channels"]["Direct Message with alexfixture"]["messages"] == 3
    assert out["channels"]["general"]["messages"] == 1  # csv layout parsed
    again = _j(server.import_export(EXPORT_ZIP))
    assert again["inserted"] == 5 and again["kept"] == 0  # upsert, no duplicates
    status = _j(server.index_status())
    assert status["sources"]["export"]["messages"] == 5
    assert status["sources"]["bot"]["messages"] == 0


def test_export_only_channels_are_not_writable():
    out = _j(server.send_message(fx.DM_CHANNEL["id"], "hi", confirm=True))
    assert "refused" in out["error"] and "export" in out["error"]
    assert "own account" in out["error"]
    out = _j(server.edit_message(fx.DM_CHANNEL["id"], "1", "hi", confirm=True))
    assert "refused" in out["error"]
    out = _j(server.delete_message(fx.DM_CHANNEL["id"], "1", confirm=True))
    assert "refused" in out["error"]
    assert not any(c[0] in ("POST", "PATCH", "DELETE") for c in FAKE.calls)


def test_sync_bot_indexes_and_is_incremental():
    out = _j(server.sync_bot())
    assert out["fetched"] == 3
    assert out["guilds"]["Fixture Guild"]["general"]["fetched"] == 2
    assert "warning" not in out
    status = _j(server.index_status())
    assert status["sources"]["bot"]["messages"] == 3
    # my own guild message existed from the export; the bot's fuller view wins
    assert status["sources"]["export"]["messages"] == 4
    assert status["bot"]["message_content_intent"] is True
    # second sync: only asks after the last snowflake, fetches nothing new
    FAKE.calls.clear()
    out = _j(server.sync_bot())
    assert out["fetched"] == 0
    gets = [c for c in FAKE.calls if c[1].endswith("/messages")]
    assert all(int(c[3]["after"]) > 0 for c in gets)
    # a new message shows up on the next sync
    FAKE.add(fx.RAIDS["id"], fx.FRIEND, "bring flasks", "2026-08-04T01:00:00+00:00")
    out = _j(server.sync_bot())
    assert out["fetched"] == 1


def test_export_never_overwrites_bot_rows():
    before = _j(server.index_status())["sources"]
    out = _j(server.import_export(EXPORT_ZIP))
    assert out["kept"] == 1 and out["inserted"] == 4
    assert _j(server.index_status())["sources"] == before


def test_search_across_sources():
    out = _j(server.search_messages("pizza"))
    assert out["count"] == 3
    srcs = {m["source"] for m in out["messages"]}
    assert srcs == {"bot", "export"}
    assert out["messages"][0]["time"] >= out["messages"][-1]["time"]  # newest first
    assert _j(server.search_messages("pizza", source="export"))["count"] == 1
    assert _j(server.search_messages("pizza", guild="Fixture"))["count"] == 2
    assert _j(server.search_messages("pizza", channel="general"))["count"] == 2
    assert _j(server.search_messages("pizza", author="alex"))["count"] == 1
    assert _j(server.search_messages("pizza", since="2026-08-02T00:00:00+00:00"))["count"] == 2
    assert _j(server.search_messages("pizza", until="2026-08-01T23:59:00+00:00"))["count"] == 1


def test_get_channel_and_dm():
    ch = _j(server.get_channel("general"))
    assert ch["source"] == "bot" and ch["count"] == 2
    assert [m["author"] for m in ch["messages"]] == ["Alex Fixture", "Fixture Me"]
    dm = _j(server.get_dm("alexfixture"))
    assert dm["count"] == 3 and dm["dms"][0]["type"] == "dm"
    assert all(m["author"] == "Fixture Me" for m in dm["messages"])
    grp = _j(server.get_dm("weekend"))
    assert grp["dms"][0]["type"] == "group"
    assert grp["messages"][0]["attachments"][0]["name"] == "map.png"
    assert "error" in _j(server.get_dm("nobody"))


def test_list_guilds_and_channels():
    g = _j(server.list_guilds())
    assert g["count"] == 1
    assert g["guilds"][0]["writable"] is True
    assert g["guilds"][0]["messages"]["bot"] == 4
    ch = _j(server.list_channels("Fixture Guild"))
    names = {c["name"]: c for c in ch["channels"]}
    assert names["raids"]["messages"] == 2 and names["raids"]["writable"]
    dms = _j(server.list_channels())
    assert {c["name"] for c in dms["channels"]} == {
        "Direct Message with alexfixture", "weekend plans"}
    assert all(not c["writable"] for c in dms["channels"])


def test_writes_require_confirm():
    for out in (
        server.send_message(fx.GENERAL["id"], "hello"),
        server.edit_message(fx.GENERAL["id"], "1", "hello"),
        server.delete_message(fx.GENERAL["id"], "1"),
    ):
        assert "confirm=true" in _j(out)["error"]
    assert not any(c[0] in ("POST", "PATCH", "DELETE") for c in FAKE.calls)


def test_send_edit_delete_as_bot_and_log():
    sent = _j(server.send_message("general", "hello from the bot", confirm=True))
    assert sent["sent"] is True
    mid = sent["message_id"]
    assert _j(server.search_messages("bot", channel="general"))["count"] == 1
    edited = _j(server.edit_message(fx.GENERAL["id"], mid, "hello again", confirm=True))
    assert edited["edited"] and edited["before"] == "hello from the bot"
    assert _j(server.get_channel("general"))["messages"][-1]["edited"]
    # cannot edit someone else's message: Discord says no, we report it
    other = FAKE.messages[fx.GENERAL["id"]][0]["id"]
    bad = _j(server.edit_message(fx.GENERAL["id"], other, "x", confirm=True))
    assert bad["edited"] is False and "403" in bad["error"]
    gone = _j(server.delete_message(fx.GENERAL["id"], mid, confirm=True))
    assert gone["deleted"] and gone["before"]["content"] == "hello again"
    row = [m for m in _j(server.get_channel("general"))["messages"] if m["id"] == mid][0]
    assert row["deleted"] is True
    log = [json.loads(l) for l in open(os.environ["DISCORD_CHANGES_LOG"])]
    actions = [(e["action"], e["result"]) for e in log]
    assert ("send", "ok") in actions and ("edit", "ok") in actions
    assert ("delete", "ok") in actions
    assert any(e["action"] == "edit" and "403" in e["result"] for e in log)


def test_index_status_flags_missing_intent():
    quiet = fx.FakeDiscord(message_content_intent=False)
    server._api = quiet
    try:
        status = _j(server.index_status())
        assert status["bot"]["message_content_intent"] is False
        assert "Message Content intent is OFF" in status["bot"]["warning"]
        assert status["bot"]["guilds"] == ["Fixture Guild"]
    finally:
        server._api = FAKE


def test_sync_local_pulls_edits_and_deletes():
    import sqlite3
    path = os.environ["DISCORD_LOCAL_DB"]
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE channels(id TEXT PRIMARY KEY, guild_id TEXT, guild_name TEXT,
            name TEXT, type INTEGER, recipients TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, guild_id TEXT, channel_id TEXT,
            author_id TEXT, author_name TEXT, content TEXT, ts INTEGER, edited_ts INTEGER,
            attachments TEXT, deleted INTEGER DEFAULT 0, rev INTEGER);
    """)
    dm = "700000000000000201"
    con.execute("INSERT INTO channels VALUES (?, NULL, NULL, 'alexfixture', 1, ?)",
                (dm, json.dumps([fx.FRIEND["id"]])))
    con.execute("INSERT INTO messages VALUES (5001, NULL, ?, ?, 'Alex Fixture', "
                "'pizza was great', 1785578400000, NULL, '[]', 0, 1)", (dm, fx.FRIEND["id"]))
    con.execute("INSERT INTO messages VALUES (5002, NULL, ?, ?, 'Alex Fixture', "
                "'see you saturday', 1785578460000, NULL, '[]', 0, 2)", (dm, fx.FRIEND["id"]))
    con.commit()
    server.LOCAL_DB = Path(path)
    try:
        out = _j(server.sync_local())
        assert out["imported"] == 2 and out["last_local_rev"] == 2
        got = _j(server.get_dm("alexfixture"))
        assert {d["source"] for d in got["dms"]} == {"export", "local"}
        assert any(m["source"] == "local" and m["author"] == "Alex Fixture" for m in got["messages"])
        # nothing new: nothing pulled
        assert _j(server.sync_local())["imported"] == 0
        # an edit and a delete bump rev on old ids and come through
        con.execute("UPDATE messages SET content = 'pizza was fine', edited_ts = 1785580200000, rev = 3 WHERE id = 5001")
        con.execute("UPDATE messages SET deleted = 1, rev = 4 WHERE id = 5002")
        con.commit()
        out = _j(server.sync_local())
        assert out["imported"] == 2 and out["last_local_rev"] == 4
        rows = {m["id"]: m for m in _j(server.get_dm("alexfixture"))["messages"]}
        assert rows["5001"]["content"] == "pizza was fine" and rows["5001"]["edited"]
        assert rows["5002"]["deleted"] is True
        status = _j(server.index_status())
        assert status["sources"]["local"]["messages"] == 2
        assert status["local_db"] == path
    finally:
        con.close()
        server.LOCAL_DB = Path(os.path.join(_TMP, "no-local.db"))
        os.remove(path)


def test_index_status_without_token():
    server._TOKEN["value"] = None
    saved = os.environ.pop("DISCORD_BOT_TOKEN")
    try:
        status = _j(server.index_status())
        assert status["bot"]["configured"] is False
        assert "no bot token" in status["bot"]["error"]
        assert "not installed" in status["local_db"]
    finally:
        os.environ["DISCORD_BOT_TOKEN"] = saved


if __name__ == "__main__":
    setup_module()
    names = [n for n in dir() if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok", n)
    print(f"{len(names)} passed")
