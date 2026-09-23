#!/usr/bin/env python3
"""Create the officers-only Great Vault channel under Progression Raid.

    scripts/create-vault-channel.py            # preview: prints the plan, changes nothing
    scripts/create-vault-channel.py --apply    # creates the channel, then reads it back

The channel carries its own permission overwrites instead of syncing with the category, so
Progression Raid's "Prog Raiders can view" does not reach it:

    @everyone   deny  View Channel
    greyBot     allow View Channel, Send Messages, Embed Links, Attach Files, History

GM and Officer hold Administrator, which Discord applies above every overwrite, so they
see it with no entry of their own. Nobody else does.

Creating the channel does not switch the report on. That takes /greybot/vault/channel_id
and the Tuesday schedule, both in docs/vault.md.
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

GUILD = "946663011991556117"
CATEGORY = "951999761358147604"          # 💀✧ Progression Raid
GREYBOT_ROLE = "1546734395115835402"
NAME = "prog-vault"
TOPIC = "Tuesday vault and gear check for Prog Raiders. Officers only."

VIEW, SEND, EMBED, ATTACH, HISTORY = 1 << 10, 1 << 11, 1 << 14, 1 << 15, 1 << 16
OVERWRITES = [
    {"id": GUILD, "type": 0, "allow": "0", "deny": str(VIEW)},
    {"id": GREYBOT_ROLE, "type": 0, "allow": str(VIEW | SEND | EMBED | ATTACH | HISTORY),
     "deny": "0"},
]


def token():
    return subprocess.check_output(
        ["aws", "--profile", "infra", "--region", "us-east-1", "ssm", "get-parameter",
         "--name", "/greybot/discord/bot_token", "--with-decryption",
         "--query", "Parameter.Value", "--output", "text"]).decode().strip()


def call(method, path, bot, body=None):
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bot {bot}", "Content-Type": "application/json",
                 "User-Agent": "greyBot (https://greybot.ryangrey.dev, 1.0)",
                 "X-Audit-Log-Reason": "greyBot: officers-only Great Vault channel"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        sys.exit(f"Discord {method} {path} answered {exc.code}: "
                 f"{exc.read().decode('utf-8', 'replace')[:300]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="create the channel")
    args = parser.parse_args()

    bot = token()
    channels = call("GET", f"/guilds/{GUILD}/channels", bot)
    category = next((c for c in channels if c["id"] == CATEGORY), None)
    if not category:
        sys.exit(f"category {CATEGORY} is gone; refusing to guess where the channel goes")
    existing = [c for c in channels if c.get("name") == NAME]
    siblings = [c for c in channels if c.get("parent_id") == CATEGORY]
    body = {"name": NAME, "type": 0, "parent_id": CATEGORY, "topic": TOPIC,
            "position": max((c.get("position", 0) for c in siblings), default=0) + 1,
            "permission_overwrites": OVERWRITES}

    print(f"category: {category['name']} ({CATEGORY})")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    if existing:
        sys.exit(f"#{NAME} already exists ({existing[0]['id']}); nothing to create")
    if not args.apply:
        print("\npreview only: nothing created. Re-run with --apply to create it.")
        return

    made = call("POST", f"/guilds/{GUILD}/channels", bot, body)
    back = call("GET", f"/channels/{made['id']}", bot)
    got = sorted((o["id"], o["allow"], o["deny"]) for o in back.get("permission_overwrites", []))
    want = sorted((o["id"], o["allow"], o["deny"]) for o in OVERWRITES)
    if back.get("parent_id") != CATEGORY or got != want:
        sys.exit(f"created {made['id']} but it reads back differently: {got}; fix it by hand")
    print(f"\ncreated #{NAME}: {made['id']} (officers only, verified)")


if __name__ == "__main__":
    main()
