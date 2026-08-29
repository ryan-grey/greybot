"""Discord webhook posting.

A webhook is the whole Discord surface this bot needs. It only ever announces -- it takes
no commands and handles no interactions -- so there is nothing for a gateway connection to
do except cost money to keep open and turn a scheduled function into a long-running
process.

The role mention is the one part with a real trap in it. A webhook message will render
`<@&123>` as a role pill whether or not the ping fires, so a mention that silently does
nothing LOOKS correct in the channel. It fires only when the role is also listed in
allowed_mentions.roles. Setting `parse: []` alongside it is what stops everything else:
with a non-empty parse list, an @everyone that ends up in a boss name would go out to the
whole server under the guild's own webhook.
"""

import json
import time
import urllib.error
import urllib.request

BRAND_NAVY = 0x0E1B2C
BRAND_ACCENT = 0x5CA8F0     # the mark's neon rim; used for ordinary kill cards
AOTC_GOLD = 0xE8B44A

MAX_ATTEMPTS = 4


class DiscordError(RuntimeError):
    pass


def post(webhook_url, payload, timeout=10, sleep=time.sleep):
    """POST one message, retrying 429 and 5xx.

    Discord answers a rate limit with retry_after in the body, so the wait is read rather
    than guessed. 4xx other than 429 is a permanent failure -- a bad webhook URL will not
    fix itself, and retrying it just delays the log line that says so.
    """
    body = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            webhook_url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "scrambled-raid-bot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.status
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {exc.code}: {text}"
            if exc.code == 429:
                try:
                    wait = float(json.loads(text).get("retry_after", 1.0))
                except (ValueError, TypeError):
                    wait = 1.0
                sleep(min(wait, 10.0))
                continue
            if 500 <= exc.code < 600:
                sleep(min(2 ** attempt, 8))
                continue
            raise DiscordError(last) from exc
        except urllib.error.URLError as exc:
            last = f"network error: {exc.reason}"
            sleep(min(2 ** attempt, 8))
    raise DiscordError(f"gave up after {MAX_ATTEMPTS} attempts: {last}")


def kill_embed(guild_name, boss_name, killed, total, raid_name, realm_rank,
               report_url=None, iso_ts=None, thumbnail_url=None):
    """The three lines from the spec, as a card.

    The rank line is omitted entirely when the rank is unknown rather than rendered as
    "Ranked server #0". Raider.IO writes 0 for "not ranked yet", and a guild that has not
    placed is not the zeroth best guild on its realm.
    """
    lines = [f"They are now **{killed}** of **{total}** in Heroic {raid_name}"]
    if realm_rank:
        lines.append(f"Ranked server **#{realm_rank}**")
    embed = {
        "title": f"{guild_name} just killed {boss_name}",
        "description": "\n".join(lines),
        "color": BRAND_ACCENT,
        "footer": {"text": f"Heroic {raid_name}"},
    }
    if report_url:
        embed["url"] = report_url
    if iso_ts:
        embed["timestamp"] = iso_ts
    # Art is decoration: a missing or dead image URL must never cost an announcement, and
    # Discord simply omits a thumbnail it cannot fetch rather than rejecting the message.
    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def aotc_payload(guild_name, raid_name, when_text, role_id, iso_ts=None,
                 thumbnail_url=None):
    """The AOTC card, and the only message in the bot that pings anyone."""
    embed = {
        "title": f"{guild_name} just got AOTC on {when_text}",
        "description": "Congratulations to the team!",
        "color": AOTC_GOLD,
        "footer": {"text": f"Ahead of the Curve — Heroic {raid_name}"},
    }
    if iso_ts:
        embed["timestamp"] = iso_ts
    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    payload = {"embeds": [embed],
               "allowed_mentions": {"parse": []}}
    if role_id:
        payload["content"] = f"<@&{role_id}>"
        payload["allowed_mentions"]["roles"] = [str(role_id)]
    return payload
