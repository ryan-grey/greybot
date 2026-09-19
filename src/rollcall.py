"""Raid-night roll call: when a team's first boss of the night dies, post who was in the kill
next to who was in the team's voice channel at that second.

WHY IT HANGS OFF THE POLL. The kill announcer only speaks for a FIRST-EVER kill, so on a farm
night it says nothing at all -- but the poll still sees every kill in the night's log. This
reads that same list, which costs no extra Warcraft Logs report query, and keeps its own
once-per-night claim so it fires on re-kills too.

WHY THE VOICE LIST IS ASKED FOR, NOT KEPT. The Lambda has no gateway connection and never sees
a voice state. The NAS service journals every join and leave, so it can answer "who was in
channel C at time T" for a T that is already up to fifteen minutes in the past -- which is
exactly how late this poll can be. One signed question, one answer.

WHO IS WHO. The operator maps Discord members to the characters they play (ROLLCALL#SETUP).
A member with no mapping falls back to a spelling guess against the kill's own names, which
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
MAX_AGE_HOURS = 6            # a kill older than this is last night's news, not a roll call
PICTURES = 40                # a raid is 30; this only bounds a pathological channel
CARD_COLOR = 0x4493F8


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, default=str))


# ------------------------------------------------------------------ which kill


def night_key(report_start_ms, tz):
    """The raid night a report belongs to. Six hours are taken off first so a raid that runs
    past midnight, or a log restarted at 12:05, is still the same night."""
    local = datetime.fromtimestamp(report_start_ms / 1000, timezone.utc).astimezone(ZoneInfo(tz))
    return (local - timedelta(hours=6)).date().isoformat()


def first_kills(by_difficulty, now_ms, tz, max_age_hours=MAX_AGE_HOURS):
    """The earliest kill of each raid night still young enough to call, oldest night first.
    `by_difficulty` is the poll's own [(difficulty, kills)]."""
    nights = {}
    for difficulty, kills in by_difficulty:
        for kill in kills:
            key = night_key(kill.get("reportStartMs") or kill["killedAtMs"], tz)
            if key not in nights or kill["killedAtMs"] < nights[key]["killedAtMs"]:
                nights[key] = {**kill, "difficulty": difficulty, "night": key}
    young = [k for k in nights.values()
             if 0 <= now_ms - k["killedAtMs"] <= max_age_hours * 3600 * 1000]
    return sorted(young, key=lambda k: k["killedAtMs"])


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
    """Give each voice member the character they had in the kill, or None.

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
            continue              # mapped, and none of their characters raided: not in the kill
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


def embed(team_name, kill, lineup, voice, image_url):
    # The public wording is the card's: "Discord", never the channel or how members were found.
    _rows, unclaimed, outside = rollcall_card.pair(lineup, voice)
    text = f"{len(lineup)} in the kill"
    if outside:
        names = ", ".join(" ".join(m["name"].split()) for m in outside)
        text += f"\n{rollcall_card.NOT_IN_KILL}: {names}"[:600]
    if unclaimed:
        text += f"\n{rollcall_card.NOT_IN_DISCORD}: {', '.join(p['name'] for p in unclaimed)}"[:600]
    return {"allowed_mentions": {"parse": []},
            "nonce": hashlib.sha256(f"rollcall:{team_name}:{kill['night']}".encode()).hexdigest()[:24],
            "enforce_nonce": True,
            "embeds": [{"title": f"Roll call · {kill['name']}", "description": text,
                        "color": CARD_COLOR, "image": {"url": image_url},
                        "footer": {"text": "greyBot · attendance at the night's first kill"}}]}


CLICK = "greybot:rollcall:"


def review_message(team, team_name, kill, payload):
    """The reviewer's copy: the same embed, plus Post and Skip. The custom_id carries everything
    the click needs to find the held payload again -- which install, which night."""
    where = f"{team or '-'}:{kill['night']}"
    return {"allowed_mentions": {"parse": []},
            "content": f"Roll call for **{team_name or 'the raid'}** is ready. Nothing has been posted. "
                       "Post sends exactly this card to the team's bot channel.",
            "embeds": payload["embeds"],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Post", "custom_id": f"{CLICK}post:{where}"},
                {"type": 2, "style": 4, "label": "Skip", "custom_id": f"{CLICK}skip:{where}"}]}]}


def run(cfg, scope, token, by_difficulty, now, tz, *, team_name, destination, publish, post,
        review=None, dry=False, max_age_hours=MAX_AGE_HOURS):
    """Post the roll call for any raid night whose first kill has just been seen.

    `publish(key, bytes)` returns the public URL; `post(destination, payload)` sends it;
    `review(user_id, payload)` DMs the reviewer when the install has one. A dry run draws and
    publishes under a preview key, claims nothing and posts nothing.
    """
    if not cfg.get("rollcall_secret") or not cfg.get("recap_page_bucket") or not cfg.get("recap_page_url"):
        return []
    setup = store.get_rollcall_setup(scope)
    if not setup or not (dry or setup.get("live")):
        return []             # saved but not switched on: previews only
    team_name = setup.get("label") or team_name
    results = []
    for kill in first_kills(by_difficulty, int(now.timestamp() * 1000), tz, max_age_hours):
        if not dry and not store.claim_rollcall(scope, kill["night"]):
            continue
        sent = False
        try:
            lineup, _rate = wcl.kill_lineup(token, kill["reportCode"], kill["encounterID"],
                                            wcl.DIFFICULTY_IDS[kill["difficulty"]])
            if not lineup:
                raise LookupError("the report does not list the kill's players yet")
            answer = ask_voice(cfg["rollcall_secret"], setup["voice_channel"], kill["killedAtMs"] / 1000)
            voice = assign(lineup, answer.get("members") or [], setup["members"])
            if not any(m["character"] for m in voice):
                # Nobody in this team's channel was in this kill: another team's night, logged
                # by someone whose reports this install also sees. The claim is KEPT, so it is
                # decided once rather than re-asked every poll.
                log("rollcall_not_this_team", team=scope.team, night=kill["night"],
                    boss=kill["name"], inVoice=len(voice))
                sent = True
                continue
            local = datetime.fromtimestamp(kill["killedAtMs"] / 1000, timezone.utc).astimezone(ZoneInfo(tz))
            sub = (f"{kill['difficulty'].capitalize()} {kill.get('zoneName') or ''}".strip()
                   + f" · {local:%a %b} {local.day} · {local.hour % 12 or 12}:{local:%M %p %Z}")
            card = rollcall_card.render(team_name, kill["name"], sub, lineup, voice, pictures(voice))
            if not card:
                raise RuntimeError("the roll call card could not be drawn")
            # Per team in both cases: two teams raid the same night, and their cards are drawn
            # in the same second by the same fan-out.
            folder = ("preview/" if dry else "") + (scope.team or "guild")
            url = publish(f"rollcall/{folder}/{kill['night']}/card-{int(now.timestamp())}.png", card)
            result = {"night": kill["night"], "boss": kill["name"], "inKill": len(lineup),
                      "inVoice": len(voice), "notInKill": sum(1 for m in voice if not m["character"]),
                      "url": url, "dry": dry}
            payload = embed(team_name, kill, lineup, voice, url)
            outcome = "rollcall_preview"
            if not dry and setup.get("review"):
                # Held, not posted: the reviewer gets it as a DM and the channel gets it only
                # if they press Post (handler.rollcall_click).
                store.put_rollcall_pending(scope, kill["night"], payload, now.isoformat())
                sent = True           # the night is decided either way: never ask twice
                review(setup["review"], review_message(scope.team, team_name, kill, payload))
                outcome, result["heldFor"] = "rollcall_held_for_review", setup["review"]
            elif not dry:
                sent = True           # from here Discord may have accepted it: never post twice
                post(destination, payload)
                outcome = "rollcall_posted"
            log(outcome, team=scope.team, **result)
            results.append(result)
        except Exception as exc:                               # noqa: BLE001
            log("rollcall_failed", team=scope.team, night=kill["night"], boss=kill["name"],
                error=repr(exc), willRetry=not sent and not dry)
        finally:
            if not dry and not sent:
                store.release_rollcall(scope, kill["night"])
    return results
