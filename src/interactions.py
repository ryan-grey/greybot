"""Discord HTTP interactions — signature verification and the /progress command.

No gateway connection here either. Discord POSTs each interaction to an HTTPS endpoint,
so a slash command is API Gateway -> Lambda like everything else.

Three things in Discord's contract are unforgiving, and each has cost people their
endpoint:

1. The signature is over (timestamp + RAW body bytes). Parsing the JSON and
   re-serialising it changes the byte sequence -- key order, separator spacing -- and
   every signature then fails in a way that looks exactly like a wrong public key. API
   Gateway may also hand the body over base64-encoded, so the encoding flag has to be
   honoured before verification, not after.

2. Discord sends deliberately INVALID signatures as a routine security probe. An endpoint
   that ever answers 200 to one is removed, with an email and a system DM about it. So the
   rejection path is a tested path here, not an assumed one.

3. The first response has a hard three-second deadline. Past it the interaction token is
   dead and the command visibly fails in the channel.

The deadline shapes the design. A Lambda cannot return a deferred response and then keep
working -- execution stops when the handler returns -- so deferring means responding type 5
and asynchronously invoking a second copy of this function to do the slow part and PATCH
the follow-up. The fast path avoids that entirely: the poller already fetches Raider.IO
every fifteen minutes, so it writes a small progress snapshot that /progress can answer
from with a single GetItem.
"""

import base64
import json
import urllib.error
import urllib.request

PING = 1
APPLICATION_COMMAND = 2

PONG = 1
CHANNEL_MESSAGE_WITH_SOURCE = 4
DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5

EPHEMERAL = 64

API_BASE = "https://discord.com/api/v10"


def raw_body(event):
    """The exact bytes Discord signed.

    API Gateway sets isBase64Encoded when it has encoded the payload. Verifying the
    encoded string rather than the decoded bytes fails every signature.
    """
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8") if isinstance(body, str) else bytes(body)


def lower_headers(event):
    return {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}


def verify(public_key, signature, timestamp, body_bytes):
    """Ed25519 check over timestamp + raw body. False on anything malformed.

    nacl is imported here rather than at module scope so the scheduled poller -- which
    shares this function and never touches interactions -- does not pay the import on
    every cold start.
    """
    if not (public_key and signature and timestamp):
        return False
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError:
        # Fail closed. An endpoint that cannot verify must reject, never wave things
        # through -- Discord probes with invalid signatures and pulls the URL if one lands.
        return False
    try:
        VerifyKey(bytes.fromhex(public_key)).verify(
            timestamp.encode("utf-8") + body_bytes, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def http(status, payload):
    return {"statusCode": status,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(payload)}


def unauthorized():
    return {"statusCode": 401,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"error": "invalid request signature"})}


def message(embed, ephemeral=True):
    data = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if ephemeral:
        data["flags"] = EPHEMERAL
    return {"type": CHANNEL_MESSAGE_WITH_SOURCE, "data": data}


def deferred(ephemeral=True):
    data = {"flags": EPHEMERAL} if ephemeral else {}
    return {"type": DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE, "data": data}


def command_name(body):
    return ((body.get("data") or {}).get("name") or "").lower()


def followup_url(application_id, token):
    return f"{API_BASE}/webhooks/{application_id}/{token}/messages/@original"


def edit_followup(application_id, token, embed, timeout=10):
    """PATCH the deferred response into its final form. The interaction token stays valid
    for fifteen minutes, so this has room even on a bad day."""
    payload = json.dumps({"embeds": [embed],
                          "allowed_mentions": {"parse": []}}).encode("utf-8")
    req = urllib.request.Request(
        followup_url(application_id, token), data=payload, method="PATCH",
        headers={"Content-Type": "application/json",
                 "User-Agent": "greybot/1.0 (+interactions)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"followup PATCH failed: HTTP {exc.code} "
                           f"{exc.read().decode('utf-8', 'replace')[:200]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"followup PATCH failed: {exc.reason}") from exc


def register_guild_commands(bot_token, application_id, guild_id, commands, timeout=15):
    """PUT the command set for one guild.

    Guild commands register instantly; global ones take up to an hour to propagate. This
    bot lives in exactly one server, so global registration would buy nothing and cost an
    hour of waiting every time the definition changes.

    PUT replaces the whole set, which makes it idempotent -- re-running cannot accumulate
    duplicates the way repeated POSTs would.
    """
    url = f"{API_BASE}/applications/{application_id}/guilds/{guild_id}/commands"
    req = urllib.request.Request(
        url, data=json.dumps(commands).encode("utf-8"), method="PUT",
        headers={"Authorization": f"Bot {bot_token}",
                 "Content-Type": "application/json",
                 "User-Agent": "greybot/1.0 (+registration)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"command registration failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"command registration failed: {exc.reason}") from exc


PROGRESS_COMMAND = {
    "name": "progress",
    "description": "Scrambled's current Heroic raid progress",
    "type": 1,
}

COMMANDS = [PROGRESS_COMMAND]


def application_id(bot_token, timeout=15):
    """The app id, derived rather than configured.

    For a bot, the user id returned by /users/@me IS the application id, so asking Discord
    is one call and removes a fourth parameter Ryan would otherwise have to find and keep
    in step with the token.
    """
    req = urllib.request.Request(
        f"{API_BASE}/users/@me",
        headers={"Authorization": f"Bot {bot_token}",
                 "User-Agent": "greybot/1.0 (+registration)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8")).get("id")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"could not read the bot user: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not read the bot user: {exc.reason}") from exc
