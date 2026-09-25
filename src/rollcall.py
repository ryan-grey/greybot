"""Raid-night roll call: when a team's first real boss pull of the night ends, kill or wipe,
post who was in the pull next to who was in the team's voice channel at that second.

WHICH PULL. The first boss pull of the night that lasted more than a minute. A pull shorter
than that is a mis-pull or a reset -- someone body-pulled the boss before the raid was
ready -- and calling attendance off it would mark half the raid absent. Wipes count: a night
can open on an hour of wipes, and attendance is about who turned up, not who got a kill.

WHY IT HANGS OFF THE POLL. The poll already runs every fifteen minutes through raid night.
The kill announcer's query only returns kills, so the roll call asks for its own small page
of boss pulls (wcl.boss_pulls_since), and only once a setup row says the install has a roll
call. It keeps its own once-per-night claim, so it fires on farm nights too.

WHY THE VOICE LIST IS ASKED FOR, NOT KEPT. The Lambda has no gateway connection and never sees
a voice state. The NAS service journals every join and leave, so it can answer "who was in
channel C at time T" for a T that is already up to fifteen minutes in the past -- which is
exactly how late this poll can be. One signed question, one answer.

WHO IS WHO. The operator maps Discord members to the characters they play (ROLLCALL#SETUP).
A member with no mapping falls back to a spelling guess against the pull's own names, which
is right often enough to be useful for a team nobody has mapped yet ("Pete (Mograin)") and is
never applied over a mapping.

OFF BY DEFAULT, twice: no shared secret means no question can be asked, and no setup row for
an install means that install has no roll call. Every failure is logged and swallowed by the
caller -- attendance never costs a kill announcement.
"""

import hashlib
import hmac
import json
import re
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import rollcall_card
import store
import team
import wcl

VOICE_URL = "https://greybot.ryangrey.dev/internal/roll-call"
USER_AGENT = "greyBot/1.0 (+https://greybot.ryangrey.dev/about)"
MAX_AGE_HOURS = 6            # a pull older than this is last night's news, not a roll call
MIN_PULL_MS = 60_000         # a pull this short is a mis-pull, never the night's roll call
PICTURES = 40                # a raid is 30; this only bounds a pathological channel
CARD_COLOR = 0x4493F8


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, default=str))


# ------------------------------------------------------------------ which pull


def night_key(report_start_ms, tz):
    """The raid night a report belongs to. Six hours are taken off first so a raid that runs
    past midnight, or a log restarted at 12:05, is still the same night."""
    local = datetime.fromtimestamp(report_start_ms / 1000, timezone.utc).astimezone(ZoneInfo(tz))
    return (local - timedelta(hours=6)).date().isoformat()


def first_pulls(pulls, now_ms, tz, max_age_hours=MAX_AGE_HOURS, min_ms=MIN_PULL_MS):
    """The earliest boss pull of each raid night that lasted longer than `min_ms` and is
    still young enough to call, oldest night first. `pulls` come from handler.fetch_pulls:
    wcl.boss_pulls_since's rows with the difficulty already named."""
    nights = {}
    for pull in pulls:
        if pull["endedAtMs"] - pull["startedAtMs"] <= min_ms:
            continue
        key = night_key(pull.get("reportStartMs") or pull["startedAtMs"], tz)
        if key not in nights or pull["startedAtMs"] < nights[key]["startedAtMs"]:
            nights[key] = {**pull, "night": key}
    young = [p for p in nights.values()
             if 0 <= now_ms - p["endedAtMs"] <= max_age_hours * 3600 * 1000]
    return sorted(young, key=lambda p: p["startedAtMs"])


# ------------------------------------------------------------------ who is who


def _fold(text):
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", text.lower())


def guess(character, display):
    """Does this display name look like it names this character? Fragments of three letters
    or more: "Pete (Mograin)" names Mograin, "Pie" names Wholepie, "Idknothin" names
    Idknothing. Deliberately loose -- it only ever runs for a member nobody has mapped."""
    c = _fold(character)
    if not c:
        return False
    for part in re.split(r"[\s/()|,]+", str(display or "")):
        p = _fold(part)
        if len(p) >= 3 and (c.startswith(p[:5]) or p in c or c in p):
            return True
        if len(p) >= 5 and sum(a == b for a, b in zip(p, c)) >= len(p) - 1:
            return True
    return False


def assign(lineup, voice, members):
    """Give each voice member the character they had in the pull, or None.

    Mappings first, for everyone, and only then guesses over what is left -- so a guess can
    never take a character that a mapped member is about to claim. One character, one member.
    """
    by_name = {team.normalize_player(p["name"]): p for p in lineup}
    taken, out = set(), {}
    for member in voice:
        for character in members.get(member["id"]) or []:
            key = team.normalize_player(character)
            if key in by_name and key not in taken:
                taken.add(key)
                out[member["id"]] = by_name[key]
                break
    for member in voice:
        if member["id"] in out or members.get(member["id"]):
            continue              # mapped, and none of their characters raided: not in the pull
        for key, row in by_name.items():
            if key not in taken and guess(row["name"], member["name"]):
                taken.add(key)
                out[member["id"]] = row
                break
    return [{**m, "character": out.get(m["id"])} for m in voice]


# ------------------------------------------------------------------ the NAS and Discord's CDN


def ask_voice(secret, channel, at_seconds, url=VOICE_URL, opener=urllib.request.urlopen):
    body = json.dumps({"channel_id": str(channel), "at": at_seconds}).encode()
    stamp = str(int(time.time()))
    digest = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT,
        "X-Greybot-Signature": f"t={stamp},v1={digest}"})
    with opener(request, timeout=8) as response:
        return json.loads(response.read(262144))


def picture_url(member):
    url = member.get("avatar_url") or ""
    if url.startswith("https://cdn.discordapp.com/"):
        return url
    # No picture of their own: Discord's coloured default, chosen the way the client chooses.
    return f"https://cdn.discordapp.com/embed/avatars/{(int(member['id']) >> 22) % 6}.png"


def pictures(voice, opener=urllib.request.urlopen):
    out = {}
    for member in voice[:PICTURES]:
        try:
            request = urllib.request.Request(picture_url(member), headers={"User-Agent": USER_AGENT})
            with opener(request, timeout=3) as response:
                out[member["id"]] = response.read(524288)
        except Exception:                                      # noqa: BLE001
            continue              # a drawn circle, not a failed roll call
    return out


# ------------------------------------------------------------------ the post


def embed(team_name, pull, lineup, voice, image_url):
    # The public wording is the card's: "Discord", never the channel or how members were found.
    _rows, unclaimed, outside = rollcall_card.pair(lineup, voice)
    text = f"{len(lineup)} in the pull"
    if outside:
        names = ", ".join(" ".join(m["name"].split()) for m in outside)
        text += f"\n{rollcall_card.NOT_IN_PULL}: {names}"[:600]
    if unclaimed:
        text += f"\n{rollcall_card.NOT_IN_DISCORD}: {', '.join(p['name'] for p in unclaimed)}"[:600]
    return {"allowed_mentions": {"parse": []},
            "nonce": hashlib.sha256(f"rollcall:{team_name}:{pull['night']}".encode()).hexdigest()[:24],
            "enforce_nonce": True,
            "embeds": [{"title": f"Roll call · {pull['name']}", "description": text,
                        "color": CARD_COLOR, "image": {"url": image_url},
                        "footer": {"text": "greyBot · attendance at the night's first pull"}}]}


CLICK = "greybot:rollcall:"


def review_message(team, team_name, pull, payload):
    """The reviewer's copy: the same embed, plus Post and Skip. The custom_id carries everything
    the click needs to find the held payload again -- which install, which night."""
    where = f"{team or '-'}:{pull['night']}"
    return {"allowed_mentions": {"parse": []},
            "content": f"Roll call for **{team_name or 'the raid'}** is ready. Nothing has been posted. "
                       "Post sends exactly this card to the team's bot channel.",
            "embeds": payload["embeds"],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Post", "custom_id": f"{CLICK}post:{where}"},
                {"type": 2, "style": 4, "label": "Skip", "custom_id": f"{CLICK}skip:{where}"}]}]}


def run(cfg, scope, token, fetch_pulls, now, tz, *, team_name, destination, publish, post,
        review=None, dry=False, max_age_hours=MAX_AGE_HOURS):
    """Post the roll call for any raid night whose first real pull has just been seen.

    `fetch_pulls(since_ms)` returns this install's boss pulls, asked for only once there is a
    setup to use them. `publish(key, bytes)` returns the public URL; `post(destination, payload)` sends it;
    `review(user_id, payload)` DMs the reviewer when the install has one. A dry run draws and
    publishes under a preview key, claims nothing and posts nothing.
    """
    if not cfg.get("rollcall_secret") or not cfg.get("recap_page_bucket") or not cfg.get("recap_page_url"):
        return []
    setup = store.get_rollcall_setup(scope)
    if not setup or not (dry or setup.get("live")):
        return []             # saved but not switched on: previews only
    team_name = setup.get("label") or team_name
    now_ms = int(now.timestamp() * 1000)
    pulls = fetch_pulls(now_ms - int(max_age_hours * 3600 * 1000))
    results = []
    for pull in first_pulls(pulls, now_ms, tz, max_age_hours):
        if not dry and not store.claim_rollcall(scope, pull["night"]):
            continue
        sent = False
        try:
            lineup, _rate = wcl.pull_lineup(token, pull["reportCode"], pull["fightID"])
            if not lineup:
                raise LookupError("the report does not list the pull's players yet")
            answer = ask_voice(cfg["rollcall_secret"], setup["voice_channel"], pull["endedAtMs"] / 1000)
            voice = assign(lineup, answer.get("members") or [], setup["members"])
            if not any(m["character"] for m in voice):
                # Nobody in this team's channel was in this pull: another team's night, logged
                # by someone whose reports this install also sees. The claim is KEPT, so it is
                # decided once rather than re-asked every poll.
                log("rollcall_not_this_team", team=scope.team, night=pull["night"],
                    boss=pull["name"], inVoice=len(voice))
                sent = True
                continue
            local = datetime.fromtimestamp(pull["startedAtMs"] / 1000, timezone.utc).astimezone(ZoneInfo(tz))
            sub = (f"{pull['difficulty'].capitalize()} {pull.get('zoneName') or ''}".strip()
                   + f" · {local:%a %b} {local.day} · {local.hour % 12 or 12}:{local:%M %p %Z}")
            card = rollcall_card.render(team_name, pull["name"], sub, lineup, voice, pictures(voice))
            if not card:
                raise RuntimeError("the roll call card could not be drawn")
            # Per team in both cases: two teams raid the same night, and their cards are drawn
            # in the same second by the same fan-out.
            folder = ("preview/" if dry else "") + (scope.team or "guild")
            url = publish(f"rollcall/{folder}/{pull['night']}/card-{int(now.timestamp())}.png", card)
            result = {"night": pull["night"], "boss": pull["name"], "wipe": not pull["kill"],
                      "inKill": len(lineup),
                      "inVoice": len(voice), "notInKill": sum(1 for m in voice if not m["character"]),
                      "url": url, "dry": dry}
            payload = embed(team_name, pull, lineup, voice, url)
            outcome = "rollcall_preview"
            if not dry and setup.get("review"):
                # Held, not posted: the reviewer gets it as a DM and the channel gets it only
                # if they press Post (handler.rollcall_click).
                store.put_rollcall_pending(scope, pull["night"], payload, now.isoformat())
                sent = True           # the night is decided either way: never ask twice
                review(setup["review"], review_message(scope.team, team_name, pull, payload))
                outcome, result["heldFor"] = "rollcall_held_for_review", setup["review"]
            elif not dry:
                sent = True           # from here Discord may have accepted it: never post twice
                post(destination, payload)
                outcome = "rollcall_posted"
            log(outcome, team=scope.team, **result)
            results.append(result)
        except Exception as exc:                               # noqa: BLE001
            log("rollcall_failed", team=scope.team, night=pull["night"], boss=pull["name"],
                error=repr(exc), willRetry=not sent and not dry)
        finally:
            if not dry and not sent:
                store.release_rollcall(scope, pull["night"])
    return results
