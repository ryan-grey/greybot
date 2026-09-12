"""Mythic+ cards and full pages using the raid recap's exact styling and fonts."""
import html
import hashlib
from urllib.parse import urlsplit

import mplus
import recap_card
import recap_page


def archived_gain(summary):
    season=summary.get('season') or {}
    return season.get('slug') == 'season-mn-2' and 1 <= season.get('week',0) <= 4


def categories(summary):
    return [(key,'Archived IO gain' if key=='score' and archived_gain(summary) else title)
            for key,title in mplus.CATEGORIES]


def label(summary):
    first = mplus.stamp(summary["start"]).astimezone(mplus.EASTERN)
    last = mplus.stamp(summary["end"]).astimezone(mplus.EASTERN)
    dates=f'{first:%b %d} – {last:%b %d, %Y}'
    season=summary.get('season')
    return f'Week #{season["week"]} · {dates}' if season else dates


def card(summary, guild):
    cells = []
    for key, title in categories(summary):
        rows = [(r["name"], r.get("class"), r.get("detail") or r.get("server"),
                 mplus.display_value(key, r), None, None, None) for r in summary["boards"][key][:3]]
        empty = "Scores unavailable" if key == 'overall' else "Baseline not available" if key == "score" and summary["score_unavailable"] else "No qualifying results"
        cells.append((title, None, rows, empty, None))
    chips = [f'{summary["timed_count"]} timed runs', f'{summary["members"]} characters',
             f'{summary["guild_count"]} full-guild runs', 'Observed runs · full details on website']
    if archived_gain(summary):chips[-1]='Archived IO gain · Approximate'
    return recap_card.render({"bossLabels":chips}, guild_name=guild, night_text=label(summary),
        raid_name=(summary.get('season') or {}).get('name','Weekly Mythic+')+' · Guild runs + overall IO',
        cells=cells, kicker="MYTHIC+ RECAP")


def safe_url(value):
    parsed = urlsplit(value or "")
    return value if parsed.scheme == "https" and parsed.hostname in ("raider.io", "www.warcraftlogs.com") and not parsed.username else ""


def page(summary, guild):
    esc = html.escape
    columns = []
    for key, title in categories(summary):
        entries = []
        for row in summary["boards"][key][:20]:
            name = esc(row["name"])
            href = safe_url(row.get("url"))
            if href:
                name = f'<a href="{esc(href, quote=True)}">{name}</a>'
            color = recap_page.class_color(row.get("class", ""))
            style = (f' style="--c-dark:{color};--c-light:{recap_page.class_color_on_light(color)}"' if color else "")
            who = f'<span class="cls"{style}>{row["rank"]}. {name}</span>'
            who += f'<small>{esc(row.get("detail") or row.get("server") or "")}</small>'
            entries.append((who, esc(mplus.display_value(key, row))))
        empty = "No qualifying results" if key != "score" else "No positive change with comparable weekly baselines"
        if key=='score' and archived_gain(summary):
            empty='Archived baseline unavailable' if summary['score_unavailable'] else 'No positive archived IO gain'
        columns.append(recap_page._column(esc(title), entries, empty))
    runs = []
    for run in summary["runs"]:
        roster = ", ".join(esc(p["name"]) + (" ★" if p["key"] in run["guild_members"] else "") for p in run["roster"])
        url = safe_url(run["url"])
        name = f'{esc(run["dungeon"])} +{run["level"]}'
        if url:
            name = f'<a href="{esc(url, quote=True)}">{name}</a>'
        elapsed = run["elapsed_ms"] // 1000
        outcome = "Timed" if run["timed"] else "Over time"
        runs.append(f'<li>{name} · {outcome} · {elapsed//60}:{elapsed%60:02d} · '
                    f'{len(run["guild_members"])}/5 guild members<br><small>{roster}</small></li>')
    title = esc(f'{guild} — {label(summary)}')
    notes = " ".join(esc(summary[k]) for k in ("coverage", "score_note", "ranking_note"))
    season=summary.get('season')
    season_text=(f'{esc(season["name"])} · Season began {mplus.stamp(season["starts"]):%b %d, %Y} · '
                 if season else '')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{title} · Mythic+ recap</title><style>{recap_page.STYLE}
.who small {{display:block;color:var(--muted);font-size:12px;white-space:normal}}
.sources li {{padding:10px 0}} .sources small {{color:var(--muted)}}
</style></head><body><header class="topbar"><a class="tb-brand" href="https://ryangrey.dev">ryangrey.dev</a><span class="lede">greyBot</span></header>
<main class="wrap"><p class="kicker">Mythic+ recap</p><h1>{title}</h1>
<p class="lede">{season_text}Tuesday 10am to Tuesday 10am Eastern · {summary["timed_count"]} observed timed runs · {summary["members"]} characters</p>
<section><h2>Guild leaderboards</h2><div class="cols">{"".join(columns)}</div><p class="note">{notes}</p></section>
<section><h2>Qualifying runs</h2><p class="note">★ Guild member at first observation · full rosters shown for every run</p><ul class="sources">{"".join(runs) or '<li>No qualifying runs collected for this week.</li>'}</ul></section>
<section><h2>Sources</h2><p class="note">Run rosters, timing and IO scores from <a href="https://raider.io">Raider.IO</a>; run links above provide the underlying results.</p></section></main>
<footer><span>Generated by greyBot</span><a href="https://ryangrey.dev">ryangrey.dev</a></footer></body></html>'''


def discord_post(summary, guild, page_url, card_url=None):
    # Discord markdown is escaped separately from HTML; never allow source mentions.
    def clean(value):
        text = str(value).replace("@", "＠")
        for ch in ("\\", "*", "_", "`", "~", "|", "[", "]"):
            text = text.replace(ch, "\\" + ch)
        return text[:110]
    fields = []
    for key, title in categories(summary):
        rows = summary["boards"][key][:3]
        text = "\n".join(f'{r["rank"]}. {clean(r["name"])} — **{mplus.display_value(key,r)}**' for r in rows)
        fields.append({"name":title, "value":text or ("Baseline unavailable" if key == "score" and summary["score_unavailable"] else "No qualifying results"), "inline":True})
    embed = {"title":f'{guild} · Weekly Mythic+', "description":label(summary) + " · Guild runs: 2+ members; overall IO: all guild characters",
             "color":0x4493F8, "fields":fields, "url":page_url,
             "author":{"name":"Raider.IO", "url":"https://raider.io"},
             "footer":{"text":"greyBot · Full standings, rosters and coverage on the website"}}
    if summary.get('season'):
        season=summary['season']
        embed['title']=f'{guild} · {season["name"]} · Week #{season["week"]}'
    if card_url:
        embed["image"] = {"url":card_url}
        # Match raid recaps: one six-panel image, without six extra mobile lists.
        embed.pop("fields")
    return {"allowed_mentions":{"parse":[]}, "embeds":[embed],
            'nonce':hashlib.sha256(('mplus-week:'+page_url).encode()).hexdigest()[:24],'enforce_nonce':True,
            "components":[{"type":1,"components":[{"type":2,"style":5,"label":"Full Mythic+ recap","url":page_url}]}]}
