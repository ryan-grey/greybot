"""Local stdio read-only tools over greyBot's guild index.

No Discord credentials or network listener are needed. Local filesystem access
is the trust boundary; do not expose this process as an unauthenticated service.
"""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from mcp.server.mcpserver import MCPServer


server = MCPServer(name="greyBot", instructions="Read-only local guild history and message search. No Discord writes.")


def connection():
    directory = Path(os.environ.get("GREYBOT_STATE_DIR", "~/.local/share/greybot-control")).expanduser().resolve()
    path = directory / "control.sqlite3"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return closing(db)


@server.tool()
def search_messages(query: str = "", channel: str = "", author: str = "", limit: int = 100) -> str:
    """Search the configured guild's private index; optional exact channel/user IDs."""
    guild = os.environ["GREYBOT_GUILD_ID"]
    clauses, args = ["guild=?", "instr(lower(content),lower(?))>0"], [guild, query]
    for field, value in (("channel", channel), ("author", author)):
        if value:
            clauses.append(field + "=?")
            args.append(value)
    args.append(max(1, min(limit, 200)))
    with connection() as db:
        result = [dict(r) for r in db.execute("SELECT * FROM messages WHERE " + " AND ".join(clauses)
                  + " ORDER BY observed DESC LIMIT ?", args)]
    return json.dumps({"messages": result, "count": len(result)})


@server.tool()
def get_channel(channel_id: str, limit: int = 100) -> str:
    """Read indexed messages for one channel; no live Discord request."""
    return search_messages(channel=channel_id, limit=limit)


@server.tool()
def read_events(user_id: str = "", kind: str = "", before: int = 0, limit: int = 100) -> str:
    """Read recorded joins, departures and audit metadata for the configured guild."""
    clauses, args = ["guild=?"], [os.environ["GREYBOT_GUILD_ID"]]
    for field, value in (("subject", user_id), ("kind", kind)):
        if value:
            clauses.append(field + "=?")
            args.append(value)
    if before:
        clauses.append("seq<?")
        args.append(before)
    args.append(max(1, min(limit, 200)))
    with connection() as db:
        result = [dict(r) for r in db.execute("SELECT * FROM events WHERE " + " AND ".join(clauses)
                  + " ORDER BY seq DESC LIMIT ?", args)]
    return json.dumps({"events": result, "count": len(result)})


@server.tool()
def index_status() -> str:
    """Report local guild coverage; counters do not certify complete Discord history."""
    guild = os.environ["GREYBOT_GUILD_ID"]
    with connection() as db:
        events = db.execute("SELECT count(*),max(observed) FROM events WHERE guild=?", (guild,)).fetchone()
        messages = db.execute("SELECT count(*) FROM messages WHERE guild=?", (guild,)).fetchone()[0]
    return json.dumps({"events": events[0], "last_event": events[1], "messages": messages,
                       "history_complete": False})


if __name__ == "__main__":
    server.run()
