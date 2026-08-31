"""Can greyBot still speak in Scrambled's Discord?

Nothing else in this bot would notice being thrown out. The announcer posts through a
webhook, and a webhook is not a member -- so a kick, a ban or a timeout leaves every poll
looking exactly like a quiet week, and the first sign of trouble would be somebody asking
why the last three kills went unannounced. These probes exist to make silence
distinguishable from being silenced.

Five questions, each asked of an endpoint that can only answer it one way:

  GET /users/@me                              the token, and our application id
  GET /applications/{a}/guilds/{g}/commands   is the app still installed in the server
  GET /users/@me/guilds                       is there a bot MEMBER in the server
  GET /guilds/{g}/members/{u}                 is that member timed out, muted, deafened
  GET {webhook_url}                           does the thing we post through still exist

INSTALLATION, not membership, is the authority on being thrown out -- and learning that
cost a false alarm on the first live run. greyBot was authorised to Scrambled with the
`applications.commands` scope and never with `bot`, so it has no member in the guild and
never has: /users/@me/guilds returns [] and /guilds/{id} answers 404 Unknown Guild.
Nothing is wrong with that. Announcements go through a webhook a person created, slash
commands arrive at the interactions endpoint, and neither needs a member. Reading absence
as a kick mailed "greyBot is no longer in the Scrambled Discord" about a bot that was
working perfectly.

So membership is REPORTED and not judged. It becomes an alert only as a regression -- a
member that existed on the last check and does not now -- which handler.py decides,
because only the stored state knows what was true before. Absence on its own is a fact
about how the app was installed, not an incident.

The webhook probe is not a formality either. Removing an app deletes the webhooks that app
created, and a channel can be deleted out from under one that was made by hand -- either
way announcements stop while every other probe still reads healthy.

A kick and a ban look identical from in here, and that is not a gap worth closing: reading
the ban list requires being in the guild, which is exactly the thing that just stopped
being true. The alert names the probe, rather than guessing at which of the two it was.

DEFINITE is the load-bearing word in this module. A 403 from the commands endpoint is a
fact. A
429, a 502 or a dropped connection is a bad minute at Discord, and mailing Ryan that he has
been banned because a TCP connection died is worse than sending nothing at all. Only
answers that can mean exactly one thing set `definite`, and handler.py only ever mails on a
definite one.
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "greybot/1.0 (+health)"

OK = "ok"
UNKNOWN = "unknown"
BAD_TOKEN = "bad_token"
NOT_INSTALLED = "not_installed"
NOT_A_MEMBER = "not_a_member"
TIMED_OUT = "timed_out"
SERVER_MUTED = "server_muted"
WEBHOOK_GONE = "webhook_gone"

# Worst first. Several of these go wrong at the same instant -- removing the app fails the
# commands fetch AND deletes any webhook the app created -- so the mail has to lead with
# the cause rather than whichever symptom happened to be probed first.
SEVERITY = [NOT_INSTALLED, NOT_A_MEMBER, WEBHOOK_GONE, BAD_TOKEN, TIMED_OUT, SERVER_MUTED]

HEADLINE = {
    NOT_INSTALLED: "greyBot has been removed from the {guild} Discord",
    NOT_A_MEMBER: "greyBot is no longer a member of the {guild} Discord",
    BAD_TOKEN: "greyBot's Discord bot token is being rejected",
    TIMED_OUT: "greyBot has been timed out in the {guild} Discord",
    SERVER_MUTED: "greyBot has been server-muted in the {guild} Discord",
    WEBHOOK_GONE: "greyBot's announcement webhook no longer exists",
}

# What Ryan should actually do about it, which is the entire reason the mail is worth
# sending. An alert that only names a state leaves the reader to go and look anyway.
ADVICE = {
    NOT_INSTALLED: ("The app's authorisation for this server is gone -- kicked, banned, or "
                    "removed from Integrations. Discord will not say which, because "
                    "reading the ban list requires being in the guild. Slash commands are "
                    "down. Announcements may still work if the webhook was made by a "
                    "person rather than by the app. Re-authorise from the Developer "
                    "Portal; if the invite bounces, it was a ban and an admin has to lift "
                    "it first."),
    NOT_A_MEMBER: ("There WAS a bot member in this server on the last check and there is "
                   "not now, so it was kicked or banned. Note this is separate from the "
                   "app being installed -- if the commands probe still reads ok, the "
                   "integration survived and only the bot user was removed."),
    BAD_TOKEN: ("The token in /greybot/discord/bot_token is not being accepted any more, "
                "which usually means it was regenerated in the Developer Portal. Slash "
                "commands are down; announcements still work, because those go through "
                "the webhook and not the token."),
    TIMED_OUT: ("A moderator has timed the app out. It lifts itself when it expires; "
                "nothing needs deploying."),
    SERVER_MUTED: ("Voice-only, so nothing this bot does is actually blocked -- greyBot "
                   "never joins voice. Worth knowing because somebody moderated the app "
                   "on purpose."),
    WEBHOOK_GONE: ("This is the one that stops announcements. Create a new webhook on the "
                   "announce channel and put its URL in /greybot/discord/webhook_url; "
                   "nothing needs redeploying, config.py re-reads it within five minutes."),
}


def _get(url, token=None, timeout=10):
    """One GET. Returns (status, body), with status None when the request never reached
    Discord at all -- which is a different thing from Discord answering 404 and must never
    be collapsed into one."""
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bot {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")[:300]
        try:
            return exc.code, json.loads(text)
        except ValueError:
            return exc.code, {"raw": text}
    except Exception as exc:                                       # noqa: BLE001
        # Timeouts, DNS, TLS, a truncated body. All of it is "we did not get an answer",
        # and none of it is evidence about the bot's standing in the server.
        return None, {"error": repr(exc)}


def _probe(name, verdict, **fields):
    return {"probe": name, "verdict": verdict, **fields}


def webhook_probe(webhook_url):
    """Does the URL the announcer posts through still resolve?

    401 and 404 are the same news wearing different hats: 404 with code 10015 is a deleted
    webhook, 401 is a URL whose token half no longer matches. Neither can post again, and
    the fix for both is a new webhook URL in SSM.
    """
    code, body = _get(webhook_url)
    body = body if isinstance(body, dict) else {}
    if code == 200:
        return _probe("webhook", OK, channelId=body.get("channel_id"))
    if code in (401, 404):
        return _probe("webhook", WEBHOOK_GONE, http=code, discordCode=body.get("code"))
    return _probe("webhook", UNKNOWN, http=code, note=body.get("error") or body.get("raw"))


def identity_probe(bot_token):
    """The token's own health, and the bot's user id.

    For a bot the user id IS the application id, so this call does double duty -- the
    member lookup needs the id, and asking Discord for it is cheaper than carrying a
    fourth parameter that can fall out of step with the token.
    """
    code, body = _get(f"{API_BASE}/users/@me", token=bot_token)
    body = body if isinstance(body, dict) else {}
    if code == 200 and body.get("id"):
        return _probe("identity", OK, userId=body["id"])
    if code == 401:
        return _probe("identity", BAD_TOKEN, http=code, discordCode=body.get("code"))
    return _probe("identity", UNKNOWN, http=code, note=body.get("error") or body.get("raw"))


def installation_probe(bot_token, application_id, guild_id):
    """Is the app still authorised for this server? The real "thrown out" signal.

    Guild commands are stored against the app's authorisation in that guild, so this
    endpoint answers 200 exactly while the app is installed and 403 Missing Access once it
    is not -- whether it was kicked, banned, or removed from the server's Integrations
    page. That holds even for an app with no bot member, which is what greyBot is.

    It is deliberately asked of a READ. Registration uses PUT against the same path, and a
    health check must never be able to change the command set it is checking.
    """
    code, body = _get(
        f"{API_BASE}/applications/{application_id}/guilds/{guild_id}/commands",
        token=bot_token)
    if code == 200 and isinstance(body, list):
        return _probe("installation", OK, commands=[c.get("name") for c in body
                                                    if isinstance(c, dict)])
    if code in (403, 404):
        detail = body.get("code") if isinstance(body, dict) else None
        return _probe("installation", NOT_INSTALLED, http=code, discordCode=detail)
    if code == 401:
        return _probe("installation", BAD_TOKEN, http=code)
    note = body.get("error") or body.get("raw") if isinstance(body, dict) else None
    return _probe("installation", UNKNOWN, http=code, note=note)


def membership_probe(bot_token, guild_id, limit=200):
    """Is there a bot MEMBER in the server? Reported, never judged.

    Absence is not a fault. An app authorised with `applications.commands` and not `bot`
    has no member and never will, which is greyBot's actual situation -- so this returns
    OK either way and carries the answer in `member`. handler.py raises NOT_A_MEMBER only
    when a member that existed before has gone, because that comparison needs the stored
    state and this function cannot see it.

    Asked of the guild list rather than of the guild. GET /guilds/{id} answers a
    non-member with 403 or 404 depending on the reason, and 404 also covers "that id was
    never a server". The list has no such ambiguity: a 200 is complete, and the id is
    either in it or it is not.

    The page-size guard is the exception. 200 is this endpoint's maximum, so a full page
    means there may be another and absence proves nothing. greyBot lives in one server and
    will never see it; it costs one comparison to be sure the day it does.
    """
    code, body = _get(f"{API_BASE}/users/@me/guilds?limit={limit}", token=bot_token)
    if code == 200 and isinstance(body, list):
        ids = {str(g.get("id")) for g in body if isinstance(g, dict)}
        if str(guild_id) in ids:
            return _probe("membership", OK, guilds=len(ids), member=True)
        if len(body) >= limit:
            return _probe("membership", UNKNOWN, guilds=len(ids),
                          note="guild list is a full page — cannot conclude absence")
        return _probe("membership", OK, guilds=len(ids), member=False)
    if code == 401:
        return _probe("membership", BAD_TOKEN, http=code)
    note = body.get("error") or body.get("raw") if isinstance(body, dict) else None
    return _probe("membership", UNKNOWN, http=code, note=note)


def _timeout_until(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def member_probe(bot_token, guild_id, user_id, now):
    """Moderation state, for a member we already know is a member.

    communication_disabled_until is a timestamp, not a flag, and it is left behind after
    it expires -- so a past date means the timeout ENDED, and reading it as a boolean
    would keep mailing about a punishment that is already over.

    `mute` and `deaf` are voice state. greyBot never joins voice, so neither one blocks
    anything it does; they are reported because somebody moderating the app on purpose is
    worth knowing about even when it costs nothing.
    """
    code, body = _get(f"{API_BASE}/guilds/{guild_id}/members/{user_id}", token=bot_token)
    body = body if isinstance(body, dict) else {}
    if code == 200:
        until = _timeout_until(body.get("communication_disabled_until"))
        if until and until > now:
            return _probe("member", TIMED_OUT, until=until.isoformat())
        if body.get("mute") or body.get("deaf"):
            return _probe("member", SERVER_MUTED, mute=bool(body.get("mute")),
                          deaf=bool(body.get("deaf")))
        return _probe("member", OK)
    if code in (403, 404):
        # membership_probe is the authority on this verdict; reaching it here as well is
        # corroboration, and matters when the guild list was the call that came back odd.
        return _probe("member", NOT_A_MEMBER, http=code, discordCode=body.get("code"))
    if code == 401:
        return _probe("member", BAD_TOKEN, http=code)
    return _probe("member", UNKNOWN, http=code, note=body.get("error") or body.get("raw"))


def check(cfg, now=None):
    """Every probe the config supports, folded into one verdict.

    The bot token and guild id are optional configuration, so this degrades rather than
    refuses: with neither of them the webhook is still checked, and the result says which
    questions were actually asked instead of implying it answered all of them.
    """
    now = now or datetime.now(timezone.utc)
    probes = []
    member = None

    if cfg.get("webhook"):
        probes.append(webhook_probe(cfg["webhook"]))

    token, gid = cfg.get("bot_token"), cfg.get("discord_guild_id")
    if token and gid:
        ident = identity_probe(token)
        probes.append(ident)
        if ident["verdict"] == OK:
            app_id = ident["userId"]
            probes.append(installation_probe(token, app_id, gid))
            seat = membership_probe(token, gid)
            probes.append(seat)
            member = seat.get("member")
            # Moderation state only exists for a member. Asking about a timeout on an app
            # that has no member returns 404 and would read as a second failure.
            if member:
                probes.append(member_probe(token, gid, app_id, now))

    bad = [p for p in probes if p["verdict"] not in (OK, UNKNOWN)]
    if bad:
        worst = min(bad, key=lambda p: SEVERITY.index(p["verdict"]))
        return {"status": worst["verdict"], "definite": True, "probes": probes,
                "cause": worst, "member": member}

    # An OK is only worth declaring when nothing was left unanswered. A half-checked bot
    # that reports healthy would clear a real alert on the strength of a timeout.
    if not probes or any(p["verdict"] == UNKNOWN for p in probes):
        return {"status": UNKNOWN, "definite": False, "probes": probes, "cause": None,
                "member": member}
    return {"status": OK, "definite": True, "probes": probes, "cause": None,
            "member": member}


ALERT, REMINDER, RECOVERY, TEST = "alert", "reminder", "recovery", "test"


def subject(kind, status, guild_name):
    if kind == RECOVERY:
        return f"greyBot is back to normal in the {guild_name} Discord"
    if kind == TEST:
        return f"greyBot health check: {status}"
    head = HEADLINE.get(status, f"greyBot: {status}").format(guild=guild_name)
    return f"Still: {head}" if kind == REMINDER else head


def body(kind, result, cfg, now_iso, since=""):
    """The mail itself. Plain text, because the forwarder sends plain text.

    Everything a decision needs is in here -- what broke, what it stops, what to do about
    it, and every probe's raw answer underneath -- because the reader is on a phone, away
    from the console, and the useful outcome is knowing whether this needs handling now or
    on Sunday.
    """
    guild = cfg.get("guild_name") or "the"
    status = result["status"]
    lines = [subject(kind, status, guild), ""]

    if kind == RECOVERY:
        lines.append("Every probe passes again.")
        if since:
            lines.append(f"It had been degraded since {since}.")
    else:
        if status != OK:
            lines += [ADVICE.get(status, "No advice recorded for this state."), ""]
        if kind == REMINDER:
            lines.append(f"Still unresolved, first seen {since}. "
                         f"A reminder, not a new event.")
        elif kind == TEST:
            lines.append("Checked by hand, not by the schedule.")
        elif since:
            lines.append(f"First seen {since}.")
    lines.append(f"Checked at {now_iso}.")

    lines += ["", "Probes:"]
    for p in result.get("probes") or []:
        extra = {k: v for k, v in p.items()
                 if k not in ("probe", "verdict") and v is not None}
        lines.append(f"  {p['probe']:<11} {p['verdict']}"
                     + (f"  {json.dumps(extra, sort_keys=True)}" if extra else ""))
    if not result.get("probes"):
        lines.append("  (none ran -- no webhook, bot token or guild id configured)")

    lines += ["",
              "Poller: ryangrey-greybot (us-east-1), every 15 minutes.",
              "Logs:   aws logs tail /aws/lambda/ryangrey-greybot --follow"]
    return "\n".join(lines)
