"""The full recap page: every raider, ranked in every category, as one static file.

The Discord card is a summary and is meant to be -- three damage names, whoever tied for
most deaths, two parses. Eighteen people raid and most of them appear nowhere on it. This
is three ranked columns with nothing dropped, and it links the Warcraft Logs reports the
numbers were read from so any line can be checked against its source.

CONSTRAINTS INHERITED FROM ryangrey.dev, deliberately, because this is served from the
same account and by the same CloudFront pattern:

  No build step        -- one self-contained file, written by the Lambda and put to S3.
  No JavaScript        -- there is no <script> tag here and nothing needs one.
  No external requests -- no CDN, no web fonts, no analytics. The palette is GitHub Primer
                          primitives vendored as literal values, matching index.html.
  System font stack    -- zero font payload, renders natively everywhere.

EVERY value that reaches the markup goes through `_esc`. Player and boss names come from
Warcraft Logs, they are typed by players, and this page is published under ryangrey.dev --
so an injection here is an injection into the site.
"""

import html
import re
import urllib.parse

# Warcraft Logs colours a parse by World of Warcraft's ITEM QUALITY scale, so these are the
# game's own quality hexes rather than anything invented here:
#
#   100      Artifact    #e6cc80    light gold
#   95-98    Legendary   #ff8000    orange
#   75-94    Epic        #a335ee    purple
#   50-74    Rare        #0070dd    blue
#   25-49    Uncommon    #1eff00    green
#   0-24     Poor        #9d9d9d    grey
#
# Verified against warcraft.wiki.gg/wiki/Quality. The ONE band that is not a WoW item
# colour is 99, which Warcraft Logs renders pink; their own pages refuse automated
# fetches, so #e268a8 below is the value the community consistently quotes rather than one
# read from source. It is the single number on this page worth checking by eye against a
# real 99 parse before this goes public.
#
# Bands are checked HIGH FIRST and 100 is its own band: an average of 99.6 is not a 100
# parse, and rounding it into the gold band would award an artifact-tier colour to a score
# nobody actually got.
PARSE_BANDS = (
    (100, "#e6cc80", "#1f2328"),
    (99,  "#e268a8", "#ffffff"),
    (95,  "#ff8000", "#ffffff"),
    (75,  "#a335ee", "#ffffff"),
    (50,  "#0070dd", "#ffffff"),
    (25,  "#1eff00", "#1f2328"),
    (0,   "#9d9d9d", "#ffffff"),
)

# World of Warcraft's own class colours, from C_ClassColor.GetClassColor() as published on
# warcraft.wiki.gg/wiki/Class_colors. Keyed on the class name folded to lowercase letters,
# because the two sources spell it differently: masterData.actors says "DeathKnight" and a
# rankings row says the same, but nothing promises a future one will not say "Death
# Knight". Folding both to "deathknight" makes the lookup independent of that.
#
# Priest is #FFFFFF, which is invisible on the light theme. It is remapped per theme in
# CSS rather than changed here -- the value in this table stays the game's.
CLASS_COLORS = {
    "deathknight": "#C41E3A", "demonhunter": "#A330C9", "druid": "#FF7C0A",
    "evoker": "#33937F", "hunter": "#AAD372", "mage": "#3FC7EB",
    "monk": "#00FF98", "paladin": "#F48CBA", "priest": "#FFFFFF",
    "rogue": "#FFF468", "shaman": "#0070DD", "warlock": "#8788EE",
    "warrior": "#C69B6D",
}


# ROLE ICONS. Drawn here as inline SVG rather than served from Blizzard's CDN, for two
# reasons that happen to point the same way: this page makes no external requests by
# design, and Blizzard's actual role textures are their art to distribute, not mine. These
# are the same three shapes the game uses -- a shield for the tank, a cross for the healer,
# crossed blades for damage -- in the role colours every WoW interface has used for years,
# so they read correctly at 14px next to a name without redistributing a game asset.
ROLE_ICONS = {
    "tank": ('<path d="M8 1.2 2.6 3v4.4c0 3.3 2.2 6 5.4 7.4 3.2-1.4 5.4-4.1 '
             '5.4-7.4V3L8 1.2Z"/>'),
    "healer": '<path d="M6.4 2h3.2v4.4H14v3.2H9.6V14H6.4V9.6H2V6.4h4.4V2Z"/>',
    "dps": ('<path d="M11.6 1.4 14.6 1l-.4 3-5 5-2.6-2.6 5-5ZM4.4 1.4 1.4 1l.4 3 '
            '5 5L9.4 6.4l-5-5ZM3.2 13.4l1.4 1.4 4-4-1.4-1.4-4 4Zm9.6 1.4 1.4-1.4-4-4'
            '-1.4 1.4 4 4Z"/>'),
}
ROLE_COLORS = {"tank": "#3f7fd4", "healer": "#3fa34d", "dps": "#c8434b"}
ROLE_LABELS = {"tank": "Tank", "healer": "Healer", "dps": "Damage"}


def role_icon(role):
    """One 14px role glyph, or a spacer so names stay aligned when the role is unknown."""
    path = ROLE_ICONS.get(role)
    if not path:
        return '<span class="role role-none" aria-hidden="true"></span>'
    return (f'<svg class="role" viewBox="0 0 16 16" role="img" '
            f'aria-label="{ROLE_LABELS.get(role, role)}" '
            f'style="fill:{ROLE_COLORS.get(role, "currentColor")}">{path}</svg>')


def class_color(name):
    """A class's colour, or None for a raider whose class could not be read."""
    key = "".join(c for c in str(name or "").lower() if c.isalpha())
    return CLASS_COLORS.get(key)


def _contrast(hex_color, against=(255, 255, 255)):
    """WCAG contrast ratio between a hex colour and a background."""
    def lum(rgb):
        chan = []
        for v in rgb:
            v /= 255.0
            chan.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]
    a, b = lum(_rgb(hex_color)), lum(against)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _rgb(hex_color):
    h = str(hex_color).lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def class_color_on_light(hex_color, target=4.0):
    """The same class colour, darkened until it is readable on white.

    Most of the game's class colours are tuned for a black UI: Priest is pure #FFFFFF and
    Rogue is #FFF468, both of which are invisible on a white page. Rather than hand-pick
    thirteen substitutes -- which is thirteen chances to invent a colour nobody recognises
    -- each one is scaled toward black only as far as it has to be to clear a contrast
    ratio. A Rogue stays recognisably yellow, a Priest becomes grey because pure white has
    nowhere else to go, and the DARK theme still gets the game's exact value.
    """
    r, g, b = _rgb(hex_color)
    scale = 1.0
    while scale > 0.05:
        candidate = "#%02x%02x%02x" % (int(r * scale), int(g * scale), int(b * scale))
        if _contrast(candidate) >= target:
            return candidate
        scale -= 0.02
    return "#1f2328"


def character_url(region, server, name):
    """A raider's Warcraft Logs page: tier progress, best parse per boss, kill counts.

    Needs all three parts. A character URL missing the realm resolves to nothing, and a
    dead link under a player's own name is worse than plain text -- so a raider whose
    server did not come back is rendered unlinked rather than linked hopefully.
    """
    if not (region and server and name):
        return None
    return (f"https://www.warcraftlogs.com/character/{_slug(region)}/"
            f"{_slug(server)}/{urllib.parse.quote(str(name))}")


def _slug(text):
    """Realm and region as Warcraft Logs writes them in a URL.

    Apostrophes are DELETED rather than turned into a separator: the realm is
    "Vek'nilash" and its slug is "veknilash", not "vek-nilash".
    """
    t = str(text or "").lower()
    t = re.sub(r"[\u2018\u2019'`]", "", t)
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


STYLE = """
:root {
  --bg:#ffffff; --ink:#1f2328; --muted:#59636e; --line:#d1d9e0; --accent:#0969da;
  --card:#ffffff; --chip:#f6f8fa; --chip-accent-bg:#ddf4ff; --topbar:#f6f8fa;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0d1117; --ink:#f0f6fc; --muted:#9198a1; --line:#3d444d; --accent:#4493f8;
    --card:#0d1117; --chip:#151b23; --chip-accent-bg:rgba(56,139,253,0.15);
    --topbar:#010409;
  }
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  background:var(--bg); color:var(--ink);
  font:16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
  -webkit-font-smoothing:antialiased;
}
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.topbar {
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  padding:12px 20px; background:var(--topbar); border-bottom:1px solid var(--line);
}
.tb-brand { color:var(--ink); font-weight:600; font-size:15px; }
.tb-brand:hover { text-decoration:none; }
.wrap { max-width:1400px; margin:0 auto; padding:32px 20px 0; }
.kicker { font-size:14px; letter-spacing:2.5px; text-transform:uppercase; color:var(--muted); }
h1 { font-size:32px; line-height:1.2; margin:6px 0 4px; letter-spacing:-0.5px; }
.lede { color:var(--muted); }
h2 {
  font-size:22px; font-weight:600; border-bottom:1px solid var(--line);
  padding-bottom:8px; margin-bottom:20px;
}
section { margin-top:48px; }
.killed { display:flex; flex-wrap:wrap; gap:8px; margin-top:16px; }
.killed span {
  font-size:13px; font-weight:500; color:var(--accent);
  background:var(--chip-accent-bg); padding:4px 12px; border-radius:999px;
}
.killed .wb {
  color:#9a6700; background:rgba(212,167,44,0.16);
  border:1px solid rgba(212,167,44,0.45);
}
@media (prefers-color-scheme: dark) { .killed .wb { color:#d29922; } }

/* Three ranked columns */
.cols { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:20px; }
@media (min-width:1180px) { .cols { grid-template-columns:repeat(5, minmax(0, 1fr)); } }
.col { border:1px solid var(--line); border-radius:6px; overflow:hidden; background:var(--card); }
.col h3 {
  display:flex; align-items:center; justify-content:center; gap:7px;
  font-size:12px; text-transform:uppercase; letter-spacing:0.6px;
  color:var(--ink); background:var(--chip); padding:10px 14px;
  border-bottom:1px solid var(--line);
}
.col h3 b { font-weight:700; }
.col h3 .role, .col h3 .skull { margin-right:0; }
.skull { font-size:13px; line-height:1; }
.parse-badge { min-width:34px; font-size:11px; line-height:18px; }
.col ol { list-style:none; }
.col li {
  display:flex; align-items:center; gap:10px;
  padding:8px 14px; border-bottom:1px solid var(--line); font-size:15px;
}
.col li:last-child { border-bottom:none; }
.role { width:14px; height:14px; flex:0 0 14px; vertical-align:-2px; margin-right:6px; }
.role-none { display:inline-block; }
.who { flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.val { flex:0 0 auto; font-variant-numeric:tabular-nums; color:var(--muted); font-size:14px; }
.empty { padding:14px; color:var(--muted); font-size:14px; }

/* Class colour: light-theme variant by default, the game's own value in dark. */
.cls { color:var(--c-light, inherit); font-weight:600; }
@media (prefers-color-scheme: dark) { .cls { color:var(--c-dark, inherit); } }
.who-link { color:inherit; }
.who-link:hover .cls { text-decoration:underline; }

/* Parse pill — GitHub tag shape, Warcraft Logs quality colour. */
.parse {
  display:inline-block; min-width:38px; text-align:center;
  font-size:12px; font-weight:600; line-height:20px; padding:0 8px;
  border-radius:999px; border:1px solid rgba(31,35,40,0.15);
}
.sub { display:block; font-size:12px; color:var(--muted); line-height:1.3; }
.sources { list-style:none; }
.sources li { padding:10px 0; border-bottom:1px solid var(--line); }
.sources li:last-child { border-bottom:none; }
.sources .meta { display:block; font-size:13.5px; color:var(--muted); }
.sources .by { color:var(--muted); }
.note { margin-top:14px; font-size:14px; color:var(--muted); }
.footnote {
  margin-top:14px; padding:10px 12px; border-left:3px solid var(--accent);
  background:var(--chip); border-radius:0 6px 6px 0; font-size:13.5px;
}
.footnote b { color:var(--ink); font-weight:600; }
.killed sup { font-size:9px; margin-left:2px; }
footer {
  max-width:1400px; margin:64px auto 0; padding:20px;
  border-top:1px solid var(--line); color:var(--muted); font-size:14px;
  display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px;
}
@media (max-width:820px) { .cols { grid-template-columns:1fr; } }
@media (max-width:560px) { h1 { font-size:26px; } .wrap { padding-top:24px; } }
"""


def _esc(value):
    """Every third-party string on this page goes through here."""
    return html.escape("" if value is None else str(value), quote=True)


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


def parse_colors(percent):
    """(background, foreground) for one parse, on Warcraft Logs' scale. See PARSE_BANDS."""
    for floor, bg, fg in PARSE_BANDS:
        if percent >= floor:
            return bg, fg
    return PARSE_BANDS[-1][1], PARSE_BANDS[-1][2]


def _pill(percent, extra=""):
    bg, fg = parse_colors(percent)
    return (f'<span class="parse{extra}" style="background:{bg};color:{fg}">'
            f'{int(round(percent))}</span>')


def _who(row, region=None, role=None):
    """A raider's name, role icon first, class-coloured, linked to their WCL page."""
    color = class_color(row.get("class"))
    # Two values, one per theme: the game's exact colour for the dark page, and a darkened
    # variant for the light one. Emitted as custom properties so the CSS picks, rather than
    # the renderer having to know which theme a reader is in.
    style = (f' style="--c-dark:{color};--c-light:{class_color_on_light(color)}"'
             if color else "")
    label = f'<span class="cls"{style}>{_esc(row["name"])}</span>'
    url = character_url(region, row.get("server"), row.get("name"))
    if url:
        label = f'<a class="who-link" href="{_esc(url)}">{label}</a>'
    if row.get("server"):
        label += f'<span class="sub">{_esc(row["server"])}</span>'
    return role_icon(role or row.get("role")) + label


def _column(title, entries, empty, icon=None, badge=None):
    """One ranked list under a centred, bolded header.

    The header used to carry a "1–20" range. It was answering a question nobody asked --
    the list already shows how many people are in it -- and it made five headers read as
    five different lengths rather than as five columns of the same thing.

    No rank numbers on the rows either. Order IS the rank in a sorted list, and a column
    of "1. 2. 3." next to a column of numbers is two rankings competing for the same eye.
    """
    if not entries:
        body = f'<p class="empty">{empty}</p>'
    else:
        items = "\n".join(
            f'<li><span class="who">{who}</span><span class="val">{val}</span></li>'
            for who, val in entries)
        body = f"<ol>{items}</ol>"
    head = (icon or "") + f"<b>{title}</b>" + (badge or "")
    return f'<div class="col"><h3>{head}</h3>{body}</div>'


def average_parse(rows):
    """The raid's overall parse: the mean of the per-raider means shown in that column.

    The mean OF THE COLUMN, deliberately, rather than the mean of every individual parse
    row. It is the number a reader can check by eye against the list underneath it; the
    other version weights the answer by how many bosses each person was present for, which
    is a different and less obvious claim to put in a header with no room to explain it.
    """
    got = [r["parseAvg"] for r in rows or () if r.get("parseAvg") is not None]
    return (sum(got) / len(got)) if got else None


def columns(rows, region=None):
    """The three ranked lists, each with its own membership rule.

    DPS      every raider with a damage figure, highest first.
    Deaths   ONLY raiders who actually died. The list stops before the zeroes rather than
             running to the bottom of the roster: "0 deaths, joint 14th" is not a ranking
             of anything, and printing it turns a clean night into filler.
    Parse    every raider who was in at least one ranked kill, best mean first. A raider
             who was in no kill has no parse to average and is absent rather than last --
             absent means "no evidence", which is true; last would be a claim.
    """
    def ranked(field, per=None):
        # `per` names a per-second field to print in front of the total, "78K/145M".
        # One cell, because DPS and damage done rank the same people and two columns
        # would say so twice; ordered by the total, which is what the card ranks on.
        def cell(r):
            rate = r.get(per) if per else None
            if isinstance(rate, (int, float)) and rate > 0:
                return f"{_short(rate)}/{_short(r[field])}"
            return _short(r[field])
        return [(_who(r, region), cell(r))
                for r in sorted((r for r in rows if r.get(field) is not None),
                                key=lambda r: (-r[field], r["name"]))]

    dps = ranked("damage", per="dps")
    heals = ranked("healing")
    taken = ranked("damageTaken")

    deaths = [(_who(r, region), str(r["deaths"]))
              for r in sorted((r for r in rows if (r.get("deaths") or 0) > 0),
                              key=lambda r: (-r["deaths"], r["name"]))]

    parses = [(_who(r, region, r.get("parseRole")), _pill(r["parseAvg"]))
              for r in sorted((r for r in rows if r.get("parseAvg") is not None),
                              key=lambda r: (-r["parseAvg"], r["name"]))]
    return dps, heals, taken, deaths, parses


def render(guild_name, raid_name, night_text, boss_labels, rows, reports,
           raiders=None, canonical=None, region=None, world_bosses=None,
           difficulty="Heroic"):
    """One night's recap page as a complete HTML document."""
    dps, heals, taken, deaths, parses = columns(rows or [], region)
    killed = "".join(f"<span>{_esc(b)}</span>" for b in boss_labels or ())
    # World bosses sit in the same chip row but marked, because they are not part of the
    # tier's count -- a reader glancing at four chips should not come away thinking the
    # guild is 5/8.
    for wb in world_bosses or ():
        diff = f" &middot; {_esc(wb['difficulty'])}" if wb.get("difficulty") else ""
        # The dagger ties the chip to the note under the columns. Without a marker the
        # two are just a chip at the top and a grey paragraph at the bottom, and a reader
        # has no reason to connect them -- which is the same as not explaining it.
        killed += (f'<span class="wb">World boss: {_esc(wb["name"])}{diff}'
                   f'<sup>&dagger;</sup></span>')
    killed_block = (f'<div class="killed">{killed}</div>' if killed
                    else '<p class="lede">No kills &mdash; a full night on progression.</p>')
    title = f"{guild_name} — {night_text}"
    canonical_tag = (f'\n<link rel="canonical" href="{_esc(canonical)}">'
                     if canonical else "")
    subtitle = f"{_esc(difficulty)} {_esc(raid_name)}"
    if raiders:
        subtitle += f" &middot; {int(raiders)} raiders"

    # Each header carries the role the column is about, in the same glyph the rows use.
    # Deaths gets a skull rather than a role, because dying is not a role.
    raid_parse = average_parse(rows or [])
    cols = "\n".join((
        _column("DPS / Damage", dps, "No damage table could be read.",
                icon=role_icon("dps")),
        _column("Healing", heals, "No healing table could be read.",
                icon=role_icon("healer")),
        _column("Damage taken", taken, "No damage-taken table could be read.",
                icon=role_icon("tank")),
        _column("Deaths", deaths, "Nobody died. Genuinely.",
                icon='<span class="skull" aria-hidden="true">\U0001F480</span>'),
        _column("Overall parse", parses,
                "No ranked kills &mdash; parses only exist for kills.",
                # The raid's own average, in the same pill the rows use. Same colours,
                # same fill: a reader should be able to tell at a glance whether the night
                # was above or below the people in the list under it.
                badge=(_pill(raid_parse, extra=" parse-badge")
                       if raid_parse is not None else None)),
    ))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} &mdash; recap</title>
<meta name="description" content="{_esc(f'{guild_name} raid recap, {night_text}. {difficulty} {raid_name}.')}">
<meta name="robots" content="noindex">{canonical_tag}
<style>{STYLE}</style>
</head>
<body>
<header class="topbar">
  <a class="tb-brand" href="https://ryangrey.dev">ryangrey.dev</a>
  <span class="lede">greyBot</span>
</header>
<main class="wrap">
  <p class="kicker">Raid recap</p>
  <h1>{_esc(title)}</h1>
  <p class="lede">{subtitle}</p>
  {killed_block}

  <section>
    <h2>Prog Raiders</h2>
    <div class="cols">
{cols}
    </div>
    <p class="note">
      {_esc(difficulty)} raid fights only &mdash; dungeons and other difficulties in the same log are
      excluded. DPS is damage done over the night&rsquo;s total fight time, as Warcraft Logs
      computes it for all fights. Overall parse is the mean of a raider&rsquo;s rankings across the kills they
      were in; bosses they sat are not counted against them.
    </p>
    <p class="note footnote">
      <sup>&dagger;</sup> <b>Every figure above excludes the world boss.</b> A world boss is
      one encounter with a full raid on it and it is not part of the tier, so counting it
      would flatter every column and mean nothing.
    </p>
  </section>

  <section>
    <h2>Source logs</h2>
    <ul class="sources">
{_sources(reports)}
    </ul>
  </section>
</main>
<footer>
  <span>Generated by greyBot</span>
  <span><a href="https://ryangrey.dev">ryangrey.dev</a></span>
</footer>
</body>
</html>
"""


def _sources(reports):
    """The logs every number above was read from, hard-linked.

    Not a courtesy. The point of publishing a leaderboard that names individuals is that
    anyone who disagrees with their line can open the log and check it, and a report code
    is not a link -- half the guild would have to be told how to turn one into a URL.

    Rendered as the log's own title in quotes, then who uploaded it:

        "READ THE GUILD NOTE" - Zatrekaz

    The quotes are load-bearing. Raiders name their logs things like READ THE GUILD NOTE
    and Starting Heroic - 8/27, and an unquoted title next to a person's name reads as a
    sentence rather than as the label somebody typed into the uploader.
    """
    out = []
    for rep in reports or ():
        url, code = rep.get("url"), rep.get("code")
        title = rep.get("title") or code or "Untitled log"
        label = f'&ldquo;{_esc(title)}&rdquo;'
        if url:
            label = f'<a href="{_esc(url)}">{label}</a>'
        owner, owner_url = rep.get("owner"), rep.get("ownerUrl")
        if owner:
            who = _esc(owner)
            if owner_url:
                who = f'<a href="{_esc(owner_url)}">{who}</a>'
            label += f' <span class="by">- {who}</span>'
        meta = " &middot; ".join(
            part for part in (_esc(code), _esc(rep.get("when") or "")) if part)
        out.append(f'<li>{label}<span class="meta">{meta}</span></li>')
    return "\n".join(out)


def guild_reports_url(guild_id):
    """Where Scrambled's uploads are listed on Warcraft Logs.

    Built from the numeric guild id the bot already holds, rather than from region/realm/
    name, because the id is the one form that cannot be broken by a realm rename or by
    however the guild name happens to be capitalised this week.
    """
    return f"https://www.warcraftlogs.com/guild/reports/{int(guild_id)}" if guild_id else None
