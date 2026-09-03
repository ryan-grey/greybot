"""The full scorecard page: every raider, every category, as one static HTML file.

The Discord card is a summary and is meant to be -- three damage names, whoever tied for
most deaths, two parses. Eighteen people raid and most of them appear nowhere on it. This
renders the same numbers with nothing dropped, and links the Warcraft Logs reports they
were read from so any figure on the page can be checked against its source.

CONSTRAINTS INHERITED FROM ryangrey.dev, deliberately, because this is served from the
same account and by the same CloudFront pattern:

  No build step        -- one self-contained file, written by the Lambda and put to S3.
  No JavaScript        -- there is no <script> tag here and nothing needs one.
  No external requests -- no CDN, no web fonts, no analytics. The palette is GitHub Primer
                          primitives vendored as literal values, matching index.html, so
                          the page is recognisably part of the same site without fetching
                          a stylesheet from anywhere.
  System font stack    -- zero font payload, renders natively everywhere.

EVERY value that reaches the markup goes through `_esc`. Player names, boss names and raid
names are third-party strings: they come from Warcraft Logs and Raider.IO, they are typed
by players, and a character named `<script>` is a perfectly legal WoW name in the sense
that matters here -- nothing upstream promises it is not. The page is published under
ryangrey.dev, so an injection here is an injection into the site.

Absent is not zero. A raider with no parse did not parse 0; they killed nothing that ranks.
Those cells render as an em dash, never a number, for the same reason the Discord card
omits a section it could not read rather than printing "unavailable".
"""

import html

# Vendored from ryangrey.dev/index.html. Kept as one literal block rather than assembled,
# so a diff against the site's own <style> is a straight read.
STYLE = """
:root {
  --bg:#ffffff; --ink:#1f2328; --muted:#59636e; --line:#d1d9e0; --accent:#0969da;
  --card:#ffffff; --chip:#f6f8fa; --chip-accent-bg:#ddf4ff; --topbar:#f6f8fa;
  --success:#1a7f37; --attention:#9a6700;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0d1117; --ink:#f0f6fc; --muted:#9198a1; --line:#3d444d; --accent:#4493f8;
    --card:#0d1117; --chip:#151b23; --chip-accent-bg:rgba(56,139,253,0.15);
    --topbar:#010409; --success:#3fb950; --attention:#d29922;
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
.wrap { max-width:1000px; margin:0 auto; padding:32px 20px 0; }
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
.tablewrap { overflow-x:auto; border:1px solid var(--line); border-radius:6px; }
table { width:100%; border-collapse:collapse; font-size:15px; }
thead th {
  text-align:left; font-size:12px; font-weight:600; text-transform:uppercase;
  letter-spacing:0.6px; color:var(--muted); background:var(--chip);
  padding:10px 14px; border-bottom:1px solid var(--line); white-space:nowrap;
}
tbody td { padding:10px 14px; border-bottom:1px solid var(--line); white-space:nowrap; }
tbody tr:last-child td { border-bottom:none; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
td.name b { font-weight:600; }
td.name em { font-style:normal; color:var(--muted); font-size:13.5px; }
.none { color:var(--muted); }
.rank { color:var(--muted); font-variant-numeric:tabular-nums; width:1%; }
.pill {
  display:inline-block; font-size:12px; font-weight:600; line-height:20px;
  padding:0 8px; border-radius:999px; background:var(--chip); border:1px solid var(--line);
}
.pill.hi { color:var(--success); }
.pill.lo { color:var(--attention); }
.sources { list-style:none; }
.sources li { padding:10px 0; border-bottom:1px solid var(--line); }
.sources li:last-child { border-bottom:none; }
.sources .meta { display:block; font-size:13.5px; color:var(--muted); }
footer {
  max-width:1000px; margin:64px auto 0; padding:20px;
  border-top:1px solid var(--line); color:var(--muted); font-size:14px;
  display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px;
}
@media (max-width:560px) { h1 { font-size:26px; } .wrap { padding-top:24px; } }
"""


def _esc(value):
    """Every third-party string on this page goes through here. See the module docstring."""
    return html.escape("" if value is None else str(value), quote=True)


def _short(n):
    """Damage as a raider reads it: 601.6M, not 601,604,882."""
    if not isinstance(n, (int, float)):
        return None
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= cutoff:
            return f"{n / cutoff:.1f}{suffix}"
    return str(int(n))


def _cell(value, css="num"):
    """A value, or an em dash. Never a 0 standing in for "not known"."""
    if value is None or value == "":
        return f'<td class="{css} none">&mdash;</td>'
    return f'<td class="{css}">{value}</td>'


def _rows(scorecard):
    out = []
    for i, r in enumerate(scorecard or (), 1):
        name = f'<b>{_esc(r["name"])}</b>'
        if r.get("server"):
            name += f'<br><em>{_esc(r["server"])}</em>'
        parse = None
        if isinstance(r.get("parse"), (int, float)):
            grade = "hi" if r["parse"] >= 75 else ("lo" if r["parse"] < 25 else "")
            parse = f'<span class="pill {grade}">{int(round(r["parse"]))}</span>'
        out.append(
            f'<tr><td class="rank">{i}</td><td class="name">{name}</td>'
            + _cell(_short(r.get("damage")))
            + _cell(r.get("deaths") if r.get("deaths") is not None else None)
            + _cell(parse)
            + _cell(_esc(r["parseBoss"]) if r.get("parseBoss") else None, css="")
            + "</tr>")
    return "\n".join(out)


def _sources(reports):
    """The logs every number above was read from, hard-linked.

    Not a courtesy. The whole point of publishing a leaderboard that names individuals is
    that anyone who disagrees with their row can open the log and check it, and a report
    code is not a link -- half the guild would have to be told how to turn one into a URL.
    """
    out = []
    for rep in reports or ():
        url, code = rep.get("url"), rep.get("code")
        title = rep.get("title") or code
        label = f'<a href="{_esc(url)}">{_esc(title)}</a>' if url else _esc(title)
        meta = " &middot; ".join(
            part for part in (_esc(code), _esc(rep.get("when") or "")) if part)
        out.append(f'<li>{label}<span class="meta">{meta}</span></li>')
    return "\n".join(out)


def render(guild_name, raid_name, night_text, boss_labels, scorecard, reports,
           raiders=None, canonical=None):
    """One night's scorecard as a complete HTML document."""
    killed = "".join(f"<span>{_esc(b)}</span>" for b in boss_labels or ())
    killed_block = (f'<div class="killed">{killed}</div>' if killed
                    else '<p class="lede">No kills &mdash; a full night on progression.</p>')
    title = f"{guild_name} — {night_text}"
    canonical_tag = (f'\n<link rel="canonical" href="{_esc(canonical)}">'
                     if canonical else "")
    subtitle = f"Heroic {_esc(raid_name)}"
    if raiders:
        subtitle += f" &middot; {int(raiders)} raiders"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} &mdash; scorecard</title>
<meta name="description" content="{_esc(f'{guild_name} raid scorecard, {night_text}. Heroic {raid_name}.')}">
<meta name="robots" content="noindex">{canonical_tag}
<style>{STYLE}</style>
</head>
<body>
<header class="topbar">
  <a class="tb-brand" href="https://ryangrey.dev">ryangrey.dev</a>
  <span class="lede">greyBot</span>
</header>
<main class="wrap">
  <p class="kicker">Raid scorecard</p>
  <h1>{_esc(title)}</h1>
  <p class="lede">{subtitle}</p>
  {killed_block}

  <section>
    <h2>Every raider</h2>
    <div class="tablewrap">
      <table>
        <thead><tr>
          <th class="rank"></th><th>Raider</th><th class="num">Damage</th>
          <th class="num">Deaths</th><th class="num">Best parse</th><th>On</th>
        </tr></thead>
        <tbody>
{_rows(scorecard)}
        </tbody>
      </table>
    </div>
    <p class="lede" style="margin-top:12px;font-size:14px">
      Damage and deaths are Heroic raid fights only &mdash; dungeons and other difficulties
      in the same log are excluded. A dash means the figure could not be read, which is not
      the same as zero.
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
