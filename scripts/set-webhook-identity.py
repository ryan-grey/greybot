#!/usr/bin/env python3
"""Set the announcing webhook's name and avatar to greyBot. Idempotent; safe to re-run.

Why the WEBHOOK and not the bot user: this bot has no gateway connection, so nothing ever
logs in as a bot user. Every announcement is an HTTP POST to a webhook, and Discord renders
those under the WEBHOOK's own name and avatar. Setting the application icon in the
Developer Portal changes the icon on the app, not the face beside the messages in #bots.
Both are worth setting; only this one changes what raiders actually see.

Set once here rather than per message. The alternative -- putting "username" and
"avatar_url" in every POST -- needs the PNG served from a publicly reachable URL, which
means hosting it somewhere, and the obvious somewhere (ryangrey.dev) is a deliberately
zero-external-request single-file site. Setting it once on the webhook sidesteps that
question entirely and keeps the announcement payloads clean.

PATCH /webhooks/{id}/{token} takes no Authorization header -- the token in the URL IS the
credential. That is also why nothing here ever prints the URL.

    scripts/set-webhook-identity.py --check     # report current identity, change nothing
    scripts/set-webhook-identity.py             # apply name + avatar
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AVATAR = os.path.join(ROOT, "assets", "greyBot-avatar.png")
BOT_NAME = "greyBot"
SSM_PARAM = "/greybot/discord/webhook_url"

# Discord documents no size cap for data-URI images, but it has historically rejected
# large ones, and a webhook avatar is never rendered above 128px. Downscaling a 1024px
# master costs nothing and removes the only plausible reason for this to fail. The 1024
# file stays the canonical asset in the repo.
MAX_UPLOAD_BYTES = 256 * 1024
UPLOAD_PX = 256

WEBHOOK_RE = re.compile(r"^https://(?:\w+\.)?discord(?:app)?\.com/api(?:/v\d+)?"
                        r"/webhooks/(\d+)/([\w-]+)/?$")


def redact(url):
    m = WEBHOOK_RE.match(url or "")
    return f"https://discord.com/api/webhooks/{m.group(1)}/***" if m else "<webhook url>"


def resolve_webhook(explicit):
    """--webhook-url, then $DISCORD_WEBHOOK_URL, then SSM. SSM is the real source; the
    other two exist so this can be run before the parameter is in place."""
    if explicit:
        return explicit.strip(), "--webhook-url"
    env = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if env:
        return env, "$DISCORD_WEBHOOK_URL"
    try:
        out = subprocess.run(
            ["aws", "ssm", "get-parameter", "--name", SSM_PARAM, "--with-decryption",
             "--query", "Parameter.Value", "--output", "text"],
            capture_output=True, text=True, check=True).stdout.strip()
    except FileNotFoundError:
        sys.exit("No webhook URL. Pass --webhook-url, or install the AWS CLI so it can be "
                 f"read from SSM {SSM_PARAM}.")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"Could not read SSM {SSM_PARAM}: {exc.stderr.strip() or exc}\n"
                 "Pass --webhook-url instead, or create the parameter "
                 "(infra/iam-setup.sh).")
    if not out or out == "None":
        sys.exit(f"SSM {SSM_PARAM} is empty.")
    return out, f"SSM {SSM_PARAM}"


def avatar_data_uri(path):
    raw = open(path, "rb").read()
    note = f"{len(raw) // 1024} KiB, uploaded as-is"
    if len(raw) > MAX_UPLOAD_BYTES and shutil.which("sips"):
        with tempfile.TemporaryDirectory() as tmp:
            small = os.path.join(tmp, "avatar.png")
            subprocess.run(["sips", "-Z", str(UPLOAD_PX), path, "--out", small],
                           capture_output=True, check=True)
            shrunk = open(small, "rb").read()
        note = (f"{len(raw) // 1024} KiB downscaled to {UPLOAD_PX}px "
                f"({len(shrunk) // 1024} KiB) for upload")
        raw = shrunk
    elif len(raw) > MAX_UPLOAD_BYTES:
        note = f"{len(raw) // 1024} KiB, uploaded as-is (sips unavailable to downscale)"
    return "data:image/png;base64," + base64.b64encode(raw).decode(), note


def call(url, method, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "User-Agent": "scrambled-raid-bot/1.0 (+greyBot identity)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        # Never echo the URL back -- it carries the token.
        sys.exit(f"Discord returned HTTP {exc.code} for {method} {redact(url)}\n{body}")
    except urllib.error.URLError as exc:
        sys.exit(f"Network error calling Discord: {exc.reason}")


def describe(hook):
    return {"name": hook.get("name"), "avatarSet": bool(hook.get("avatar")),
            "avatar": hook.get("avatar"), "channelId": hook.get("channel_id"),
            "guildId": hook.get("guild_id")}


def main():
    ap = argparse.ArgumentParser(description="Set the announcing webhook's greyBot identity.")
    ap.add_argument("--webhook-url", help="overrides $DISCORD_WEBHOOK_URL and SSM")
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    ap.add_argument("--name", default=BOT_NAME)
    ap.add_argument("--avatar", default=AVATAR)
    args = ap.parse_args()

    if not os.path.exists(args.avatar):
        sys.exit(f"Avatar not found: {args.avatar}")

    url, source = resolve_webhook(args.webhook_url)
    if not WEBHOOK_RE.match(url):
        sys.exit("That does not look like a Discord webhook URL "
                 "(https://discord.com/api/webhooks/<id>/<token>).")

    print(f"webhook: {redact(url)}  (from {source})")
    before = call(url, "GET")
    print("before:  " + json.dumps(describe(before)))

    if args.check:
        ok = before.get("name") == args.name and bool(before.get("avatar"))
        print("\n" + ("identity is already greyBot with an avatar set."
                      if ok else
                      "identity is NOT set — run without --check to apply."))
        return 0 if ok else 1

    data_uri, note = avatar_data_uri(args.avatar)
    print(f"avatar:  {os.path.relpath(args.avatar, ROOT)} — {note}")

    after = call(url, "PATCH", {"name": args.name, "avatar": data_uri})
    print("after:   " + json.dumps(describe(after)))

    if after.get("name") != args.name or not after.get("avatar"):
        sys.exit("Discord accepted the request but the identity did not stick.")
    print(f"\nAnnouncements in #bots will now post as {args.name} with the greyBot avatar.")
    print("The application icon in the Developer Portal is separate — see README.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
