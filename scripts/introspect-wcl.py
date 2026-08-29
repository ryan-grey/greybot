#!/usr/bin/env python3
"""Ask Warcraft Logs what its schema actually is, rather than assuming.

Run this BEFORE writing any new query. Two questions it exists to answer:

  1. Do report tags (GuildTag) exist, and can reports() filter on one? If Scrambled
     tags its two raid teams, the tag is a far better team signal than guessing from
     roster overlap, and everything else in team.py becomes a fallback.

  2. What arguments do table/rankings/fights/masterData really take? Those three
     return the untyped JSON scalar, so the ARGUMENTS are the only part of them the
     schema can vouch for -- and getting an argument wrong is a 400, not a warning.

Credentials come from the environment if set, otherwise from SSM. Run it in CloudShell,
where the admin identity can read /greybot/wcl/*; the ryan-cli deploy identity
deliberately cannot, which is the whole point of that separation.

  WCL_CLIENT_ID=... WCL_CLIENT_SECRET=... python3 scripts/introspect-wcl.py
  python3 scripts/introspect-wcl.py            # reads SSM

Introspection is a schema read: it touches no guild data and posts nothing.
"""

import base64
import json
import os
import sys
import urllib.parse
import urllib.request

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

# Types worth asking about, and why each one is on the list.
TARGETS = [
    ("ReportData", "does reports() take a tag/team filter argument?"),
    ("Report", "does a report expose the tag it was filed under?"),
    ("ReportFight", "participant list for a kill -- friendlyPlayers, or something else"),
    ("ReportMasterData", "actor id -> name/server, needed to read any participant list"),
    ("ReportActor", "does an actor carry guild membership? decides the pug filter rule"),
    ("GuildData", "guild lookup arguments"),
    ("Guild", "does a guild expose its tags/teams?"),
    ("GuildTag", "the tag type itself, if it exists at all"),
]

# Full argument detail only for the fields whose arguments actually matter. Printing
# every argument of every field on Report is unreadable and unpasteable.
ARGS_FOR = {"reports", "table", "rankings", "fights", "events", "masterData", "guild",
            "playerDetails", "graph"}

TYPE_REF = """
  type { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
"""

QUERY = """
query Introspect {
  %s
}
""" % "\n  ".join(
    '%s: __type(name: "%s") { name kind fields { name %s args { name %s } } '
    'inputFields { name %s } enumValues { name } }'
    % (t.lower(), t, TYPE_REF, TYPE_REF, TYPE_REF)
    for t, _why in TARGETS
)


def post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code} from {url}: {exc.read().decode('utf-8', 'replace')[:500]}")


def credentials():
    cid = os.environ.get("WCL_CLIENT_ID", "").strip()
    sec = os.environ.get("WCL_CLIENT_SECRET", "").strip()
    if cid and sec:
        return cid, sec, "environment"
    import boto3
    ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    res = ssm.get_parameters(Names=["/greybot/wcl/client_id", "/greybot/wcl/client_secret"],
                             WithDecryption=True)
    got = {p["Name"]: p["Value"] for p in res.get("Parameters", [])}
    if len(got) != 2:
        sys.exit("could not read /greybot/wcl/* from SSM — run this in CloudShell as admin, "
                 "or export WCL_CLIENT_ID and WCL_CLIENT_SECRET")
    return (got["/greybot/wcl/client_id"].strip(),
            got["/greybot/wcl/client_secret"].strip(), "SSM")


def render(ref, depth=0):
    """GraphQL type refs nest NonNull and List wrappers around the real type."""
    if not ref or depth > 6:
        return "?"
    kind, name = ref.get("kind"), ref.get("name")
    if name:
        return name
    inner = render(ref.get("ofType"), depth + 1)
    return f"[{inner}]" if kind == "LIST" else f"{inner}!" if kind == "NON_NULL" else inner


def main():
    cid, sec, source = credentials()
    print(f"credentials: {source}\n")
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    tok = post(TOKEN_URL, urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
               {"Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded"}).get("access_token")
    if not tok:
        sys.exit("no access_token in the token response")

    payload = post(API_URL, json.dumps({"query": QUERY}).encode(),
                   {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    if payload.get("errors"):
        print("GraphQL errors:", json.dumps(payload["errors"])[:800])
    data = payload.get("data") or {}

    for name, why in TARGETS:
        t = data.get(name.lower())
        print("=" * 72)
        print(f"{name}  —  {why}")
        if not t:
            print("  DOES NOT EXIST in the schema")
            continue
        for f in t.get("fields") or []:
            line = f"  {f['name']}: {render(f.get('type'))}"
            args = f.get("args") or []
            if args and f["name"] in ARGS_FOR:
                line += "\n      args: " + ", ".join(
                    f"{a['name']}: {render(a.get('type'))}" for a in args)
            elif args:
                line += f"   ({len(args)} args)"
            print(line)
        for f in t.get("inputFields") or []:
            print(f"  (input) {f['name']}: {render(f.get('type'))}")
        vals = [v["name"] for v in (t.get("enumValues") or [])]
        if vals:
            print("  enum: " + ", ".join(vals))

    # The single question this whole script exists to settle, answered in one line.
    print("=" * 72)
    blob = json.dumps(data).lower()
    hits = [w for w in ("guildtag", "tagid", "guildtagid", '"tags"') if w in blob]
    print("TAG SUPPORT:", ", ".join(hits) if hits else
          "no tag/guildTag reference anywhere in the introspected types")


if __name__ == "__main__":
    main()
