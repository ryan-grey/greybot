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

# How many still-standing bosses the card will name. A night that touched five and killed
# none is a real thing, and five pull lines under a two-line summary is not a card any
# more. The full picture is what the recap page is for.
MAX_UNKILLED_ON_CARD = 3


# NO ANSI BLOCK, BY CHOICE. Discord will colour text only inside a fenced ```ansi block,
# and a code block will not render a custom server emoji -- so the card can have colour or
# it can have real role artwork, never both. Role icons won.
#
# What that buys back is markdown: outside a code block the names can be bold, so rank and
# name still separate from the number below them without a monospace grid doing the work.
#
# ROLE_EMOJI is unicode so it works on day one with nothing set up. To swap in the actual
# WoW role icons, upload them to the server as emoji and put their Discord ids here in the
# form "<:tank:123456789>" -- everything else on the card stays exactly as it is.
ROLE_EMOJI = {"tank": "\U0001F6E1\uFE0F", "healer": "\U0001F49A",
              "dps": "\u2694\uFE0F"}
ROLE_BLANK = "\u2003"

# Three inline fields fill one row on desktop, which is what makes the card read as
# columns rather than as a list. More than three per row is not a layout Discord offers.
TOP_N = 3


def _column(rows):
    """One category as a ranked list, ONE LINE PER ENTRY.

    The first version put the number on its own line under the name, indented. It cannot
    work: an embed field renders in a proportional font, so leading spaces buy no alignment
    at all -- three entries indented by the same two spaces still start at three different
    x positions, and the result reads as ragged rather than as a column.

    So there is nothing left to align. Name and number share a line separated by an em
    dash, which lines up by construction because every row is the same shape. Field values
    are a third of the embed wide on desktop and full width on mobile, and "Deathbrewst
    602M" fits both.
    """
    if not rows:
        return None
    return "\n".join(
        f"{ROLE_EMOJI.get(role, ROLE_BLANK)} `{i}.` {name} — **{value}**"
        for i, (name, _klass, value, role) in enumerate(rows, 1))


def _field(label, rows):
    block = _column(rows)
    return [{"name": label, "value": block, "inline": True}] if block else []


def _pad_rows(fields):
    """Fill the last row of inline fields out to three.

    Discord lays inline fields three to a row and STRETCHES a short final row to fill the
    width, so five fields render as three normal columns and then two wide ones. A blank
    field is not decoration here: it is what keeps the second row on the same grid as the
    first. The name is a zero-width space because Discord rejects an empty one.
    """
    remainder = len(fields) % 3
    if remainder:
        fields += [{"name": "\u200b", "value": "\u200b", "inline": True}
                   for _ in range(3 - remainder)]
    return fields


def _join(items):
    """"a", "a and b", "a, b and c"."""
    items = [f"**{i}**" for i in items]
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


class DiscordError(RuntimeError):
    pass


class _Posted(int):
    """The HTTP status, with the created message's ids hung off it.

    An int subclass rather than a tuple or a dict because every existing caller treats the
    return of a post as a status code and several of them compare it numerically. Widening
    the type would have meant finding all of them and being wrong about one.
    """
    message_id = None
    channel_id = None


def post(webhook_url, payload, timeout=10, sleep=time.sleep):
    """POST one message to a webhook, retrying 429 and 5xx.

    `?wait=true` is appended so Discord answers with the created message rather than a
    bare 204. That one query parameter is what makes "did somebody delete our post"
    answerable at all: without the message id there is nothing to ask about later, and
    Discord will not tell you after the fact.
    """
    sep = "&" if "?" in webhook_url else "?"
    return _post_json(f"{webhook_url}{sep}wait=true", payload,
                      timeout=timeout, sleep=sleep)


def _post_json(url, payload, headers=None, timeout=10, sleep=time.sleep):
    """POST one message, retrying 429 and 5xx.

    Discord answers a rate limit with retry_after in the body, so the wait is read rather
    than guessed. 4xx other than 429 is a permanent failure -- a bad webhook URL will not
    fix itself, and retrying it just delays the log line that says so.

    Shared by the webhook and bot-token paths so that the retry behaviour cannot
    drift between them. A rate limit answered correctly on one destination and
    guessed at on the other is the kind of difference that only shows up under
    load, which is exactly when it matters.
    """
    body = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "scrambled-raid-bot/1.0",
                     **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read().decode("utf-8", "replace")
                try:
                    body = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    body = {}
                # The status is what every existing caller reads, so it stays the return
                # value and the message rides along as an attribute. A 204 with no body
                # still returns cleanly -- posting must not start failing because the
                # bookkeeping around it could not read an id.
                out = _Posted(res.status)
                if isinstance(body, dict):
                    out.message_id = body.get("id")
                    out.channel_id = body.get("channel_id")
                return out
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


def _author(guild_label, guild_url, icon_url=None):
    """Attribution, not promotion -- and the only left-hand image slot an embed has.

    Raider.IO require a link back from anything public using their data, and a footer
    cannot carry one -- embed footers render as plain text, so a link there is dead
    characters. The author block is the one place a link fits without touching the three
    lines of the message itself, and it happens to be useful: one click to the guild's
    progress page.

    `icon_url` is where the boss art goes when it is wanted on the LEFT. Discord's embed
    layout is fixed and offers exactly two image slots -- `thumbnail`, always top-right,
    and `image`, always full width underneath -- with no field controlling the position or
    the rendered size of either. The docs are explicit that a thumbnail's height and width
    are metadata Discord returns, not values a sender sets. So the author icon is not a
    smaller version of the thumbnail idea, it is the only alternative that exists: a ~20px
    circle on the left of the author line.
    """
    if not guild_url:
        return None
    author = {"name": guild_label or "Raider.IO", "url": guild_url}
    if icon_url:
        author["icon_url"] = icon_url
    return author


def kill_embed(guild_name, boss_name, killed, total, raid_name, realm_rank,
               report_url=None, iso_ts=None, thumbnail_url=None,
               guild_label=None, guild_url=None, world_boss=False, card_url=None):
    """The three lines from the spec, as a card.

    The rank line is omitted entirely when the rank is unknown rather than rendered as
    "Ranked server #0". Raider.IO writes 0 for "not ranked yet", and a guild that has not
    placed is not the zeroth best guild on its realm.

    A WORLD BOSS gets a different middle line. Raider.IO carries one as a raid with a
    single encounter, so the ordinary progress line comes out as "now 1 of 1 in Heroic The
    Tidebound Grotto" -- technically true, and it reads like a bug. The kill is the news;
    the progress fraction is not.
    """
    if world_boss:
        lines = [f"**World boss** &mdash; killed on **Heroic**".replace("&mdash;", "—")]
    else:
        lines = [f"They are now **{killed}** of **{total}** in Heroic {raid_name}"]
    if realm_rank and not world_boss:
        lines.append(f"Ranked server **#{realm_rank}**")
    embed = {
        "title": f"{guild_name} just killed {boss_name}",
        "description": "\n".join(lines),
        "color": BRAND_ACCENT,
        "footer": {"text": ("World boss" if world_boss else f"Heroic {raid_name}")},
    }
    if report_url:
        embed["url"] = report_url
    if iso_ts:
        embed["timestamp"] = iso_ts
    if card_url:
        # The drawn card carries the text, so the embed drops its own copy of it rather
        # than saying everything twice. Title stays: it is the clickable link to the log,
        # and an image cannot be clicked.
        embed["image"] = {"url": card_url}
        embed.pop("description", None)
        embed.pop("thumbnail", None)
    # Art is decoration: a missing or dead image URL must never cost an announcement, and
    # Discord simply omits a thumbnail it cannot fetch rather than rejecting the message.
    elif thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    author = _author(guild_label, guild_url)
    if author:
        embed["author"] = author
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def aotc_payload(guild_name, raid_name, when_text, role_id, iso_ts=None,
                 thumbnail_url=None, guild_label=None, guild_url=None, repo_url=None,
                 card_url=None):
    """The AOTC card, and the only message in the bot that pings anyone.

    This is also the only card carrying a credit line. A kill card goes out several times
    a tier into a channel shared with the raid team, and a developer plug on every one of
    them is noise in someone else's room. AOTC fires once per tier and is celebratory,
    which is the one moment where a small "built by" reads as charm rather than adverts.
    """
    description = "Congratulations to the team!"
    if repo_url:
        description += f"\n\n[greyBot]({repo_url})"
    embed = {
        "title": f"{guild_name} just got AOTC on {when_text}",
        "description": description,
        "color": AOTC_GOLD,
        "footer": {"text": f"Ahead of the Curve — Heroic {raid_name}"},
    }
    if iso_ts:
        embed["timestamp"] = iso_ts
    if card_url:
        # The card says the guild, the achievement, the raid and the date, so the embed
        # stops saying all four a second time. What it keeps is the credit link: a PNG
        # cannot be clicked, and unlike a kill card the title here carries no URL to
        # survive as one, so the line goes in the description or nowhere.
        embed["image"] = {"url": card_url}
        embed.pop("thumbnail", None)
        embed.pop("title", None)
        if repo_url:
            embed["description"] = f"[greyBot]({repo_url})"
        else:
            embed.pop("description", None)
    elif thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    author = _author(guild_label, guild_url)
    if author:
        embed["author"] = author
    payload = {"embeds": [embed],
               "allowed_mentions": {"parse": []}}
    if role_id:
        payload["content"] = f"<@&{role_id}>"
        payload["allowed_mentions"]["roles"] = [str(role_id)]
    return payload


def progress_embed(guild_name, raid_name, killed, total, realm_rank,
                   thumbnail_url=None, guild_label=None, guild_url=None, as_of=None):
    """The /progress card.

    Deliberately the same colour, author block and thumbnail treatment as a kill card, so
    the two read as one product rather than two tools that happen to share a channel.
    """
    line = (f"**{guild_name}** \u2014 **{killed}** of **{total}** "
            f"in Heroic {raid_name}")
    if realm_rank:
        line += f", ranked server **#{realm_rank}**"
    embed = {"description": line, "color": BRAND_ACCENT,
             "footer": {"text": f"Heroic {raid_name}"
                                + (f" \u00b7 as of {as_of}" if as_of else "")}}
    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    author = _author(guild_label, guild_url)
    if author:
        embed["author"] = author
    return embed


def _short(n):
    """Damage totals, at a length a phone can read.

    565,524,499 is nine characters of noise in a field three of these have to share, and
    nobody reads the units digit of a damage meter -- so it rounds to a whole unit: 602M,
    not 601.6M. The tenth was never information, it was just width.

    Rounding is done BEFORE the suffix is chosen, which is what stops 999.6M rendering as
    "1000M": once it rounds up to a full thousand of its own unit it is promoted to the
    next one and comes out as 1B.
    """
    if not isinstance(n, (int, float)):
        return ""
    n = float(n)
    for cutoff, suffix, bigger in ((1e12, "T", None), (1e9, "B", "T"),
                                   (1e6, "M", "B"), (1e3, "K", "M")):
        if n >= cutoff:
            value = round(n / cutoff)
            if value >= 1000 and bigger:
                return f"1{bigger}"
            return f"{value}{suffix}"
    return str(int(round(n)))


def _tied(rows, key):
    """Everyone level at the top, not just whoever sorted first.

    Ties are real and common: the captured night ended with three people on fifteen deaths
    each. Naming one of them is a quiet lie in a card people will argue about, and which
    one gets named would come down to the alphabet.
    """
    if not rows:
        return []
    best = rows[0].get(key)
    return [r for r in rows if r.get(key) == best]


def recap_embed(guild_name, raid_name, night_text, summary, report_url=None, iso_ts=None,
                thumbnail_url=None, guild_label=None, guild_url=None, recap_url=None):
    """The morning-after card. One embed, no ping, same visual language as a kill card.

    Every section is optional and silently absent when it could not be read. A recap that
    lost its rankings blob is a card without a parse line, not a card that says "parse
    unavailable" -- an apology for missing data is worse than not mentioning it, and worse
    than the alternative of failing the whole post.
    """
    lines = []
    # THE COUNT FIRST, THEN THE PROGRESSION. Listing every kill answered neither question
    # a reader has: an eight-boss farm clear and a night that finally broke through read
    # exactly the same. The count says how much fell; the "including first kills on" clause
    # says how much of it was new, and it is simply absent on a night that was all farm.
    #
    # Labels carry the tier position ("3/8 Entombed Sentinels"); bare names are the
    # fallback for a tier whose encounter list could not be read. This module stays free of
    # raiderio -- numbering is decided in recap.summarise and arrives here as text.
    killed = summary.get("killed")
    if killed is None:
        killed = len(summary.get("bosses") or [])
    firsts = summary.get("firstKillLabels") or summary.get("firstKills") or []

    # The world boss is COUNTED, not named. The card names two things and both are
    # things the guild has to do again: a first kill, and a boss still standing. A world
    # boss is neither -- it is the same tag every week -- so it earns a clause and not a
    # name. The page still names it, where there is room to be complete.
    world = [w["name"] for w in (summary.get("worldBosses") or []) if w.get("name")]
    world_clause = ""
    if world:
        world_clause = " & the world boss" if len(world) == 1 else \
                       f" & {len(world)} world bosses"

    if killed:
        line = (f"{guild_name} killed **{killed}** Heroic "
                f"{'boss' if killed == 1 else 'bosses'}{world_clause}")
        if firsts:
            line += ", including first kills on " + _join(firsts)
        lines.append(line)
    elif world:
        # No tier kills at all, but the world boss died. Saying "no kills" here would be
        # flatly untrue, so the sentence starts from what did happen.
        lines.append(f"{guild_name} killed the "
                     f"{'world boss' if len(world) == 1 else f'{len(world)} world bosses'}")
    else:
        lines.append("No kills — a full night on progression")

    # Pulls on what is STILL STANDING. A pull count on a boss that died is a statistic
    # about a solved problem; on a boss that did not, it is the story of the night.
    for boss in (summary.get("unkilled") or [])[:MAX_UNKILLED_ON_CARD]:
        line = (f"**{boss['pulls']}** "
                f"{'pull' if boss['pulls'] == 1 else 'pulls'} on "
                f"{boss.get('label') or boss['name']}")
        # fightPercentage is REMAINING health, so a low number is a close attempt.
        if isinstance(boss.get("best"), (int, float)) and boss["best"] > 0:
            line += f" — best **{boss['best']:.1f}%**"
        lines.append(line)

    embed = {
        "title": f"{guild_name} — {night_text}",
        "description": "\n".join(lines),
        "color": BRAND_ACCENT,
        "fields": [],
        "footer": {"text": f"Heroic {raid_name}"
                           + (f" · {summary['raiders']} raiders"
                              if summary.get("raiders") else "")},
    }

    # Five categories, three to a row. Damage / heals / damage taken is one row and reads
    # as the night's output; deaths and parse is the next and reads as how it went.
    for label, key in (("Top damage", "damage"), ("Top heals", "healing"),
                       ("Damage taken", "damageTaken")):
        embed["fields"] += _field(label, [
            (r["name"], r.get("class"), _short(r["total"]), r.get("role"))
            for r in (summary.get(key) or [])[:TOP_N]])

    embed["fields"] += _field("Most deaths", [
        (r["name"], r.get("class"), str(r["deaths"]), r.get("role"))
        for r in (summary.get("deaths") or [])[:TOP_N]])

    # The parse column is coloured by the PARSE, not by the class -- it is the one number
    # on the card where the colour is the reader's shorthand for the value.
    parses = summary.get("parses") or {}
    for key, label in (("best", "Best parse"), ("worst", "Worst parse")):
        pr = parses.get(key)
        if not pr:
            continue
        pct = int(round(pr["percent"]))
        embed["fields"].append({
            "name": label,
            "value": (f"{ROLE_EMOJI.get(pr.get('role'), ROLE_BLANK)} "
                      f"{pr['name']} — **{pct}**\n{pr['boss']}"),
            "inline": True})

    if embed["fields"]:
        # Padded to a full row FIRST, then the link is appended as a full-width field.
        # Order matters: a non-inline field ends the current row, so appending it before
        # padding would leave the last two columns stretched again.
        embed["fields"] = _pad_rows(embed["fields"])
        if recap_url:
            # A field, not a second embed and not a line in the description. The
            # description renders ABOVE the columns, and a second embed reads as a second
            # post -- a full-width field is the only slot inside one card that is both
            # below the columns and still able to render a link. The footer cannot: it
            # renders neither markdown nor links.
            embed["fields"].append({"name": "\u200b",
                                    "value": f"Full recap here: {recap_url}",
                                    "inline": False})
    elif recap_url:
        embed["fields"] = [{"name": "\u200b",
                            "value": f"Full recap here: {recap_url}",
                            "inline": False}]
    else:
        embed.pop("fields")
    if report_url:
        embed["url"] = report_url
    if iso_ts:
        embed["timestamp"] = iso_ts
    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    author = _author(guild_label, guild_url)
    if author:
        embed["author"] = author
    # No content, no roles in allowed_mentions. The recap is informational -- the AOTC card
    # remains the only thing in this bot that pings anybody.
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}

CHANNEL_API = "https://discord.com/api/v10/channels"


def post_to(destination, payload, timeout=10, sleep=time.sleep):
    """POST one announcement to wherever this install posts.

    Two destinations, one call site. A single-tenant install posts through the
    webhook URL it has always used; a configured tenant posts to its channel with
    the bot token.

    Channel posting rather than a webhook per tenant, deliberately. Creating a
    webhook per install would mean MANAGE_WEBHOOKS on every server and a secret
    URL stored per tenant -- a second credential to hold, rotate and leak. The bot
    token is already held once, centrally, and `Send Messages` in the chosen
    channel is the smallest permission that does the job.

    `destination` is either {"webhook": url} or {"bot_token": t, "channel": id}.
    """
    if destination.get("webhook"):
        return post(destination["webhook"], payload, timeout=timeout, sleep=sleep)

    token = destination.get("bot_token")
    channel = destination.get("channel")
    if not token or not channel:
        raise DiscordError("no destination configured: need a webhook, "
                           "or a bot token and channel id")
    return _post_json(f"{CHANNEL_API}/{channel}/messages", payload,
                      headers={"Authorization": f"Bot {token}"},
                      timeout=timeout, sleep=sleep)
