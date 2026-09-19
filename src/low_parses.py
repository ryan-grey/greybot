"""The night's grey parses, sent privately to one person with the recap.

WHY THIS IS A DM AND NOT A CARD. `show_worst_parse` has been opt-in since the recap was
written, with the reason in the config: parse-shaming starts arguments. Naming several
people's bad nights in a channel would be that, louder. This goes to one person, who asked
for it, so it can be used to coach rather than to embarrass. It has no channel path at all
-- there is nowhere to accidentally point it.

WHAT COUNTS. Ryan chose grey only: below 25 rankPercent, the bottom quarter of everyone
logged on that boss and spec. Green is left alone. The threshold is configuration rather
than a constant so it can move without a deploy.

The rows come from `recap.parse_rows`, which the recap has already built and filtered to
this night's fights and difficulty -- so this can never disagree with the card about whose
parses counted, and costs no extra Warcraft Logs query.

Bosses are numbered by their position in Raider.IO's ordered encounter list for the tier,
which is the sense in which a raider says "boss 4": how deep in the raid it is. A boss the
list does not name keeps its name and loses only its number.
"""

MAX_LINES = 40          # a DM is 2000 characters; a pathological night is truncated, not lost
COLOR = 0x9198A1        # Primer's muted grey, the colour of the parses it is reporting


def boss_numbers(encounters):
    """{boss name: 1-based position in the tier}. Case-folded, because Warcraft Logs and
    Raider.IO do not always agree on capitalisation."""
    return {str(name).casefold(): index
            for index, name in enumerate(encounters or (), start=1) if name}


def collect(rows, encounters=None, threshold=25.0):
    """Grey parses, grouped by boss, bosses in tier order and worst first within each.

    One entry per character per kill, which is what the raider did: two greys on two bosses
    is two lines, because it is two performances.
    """
    numbers = boss_numbers(encounters)
    out = []
    for row in rows or ():
        percent = row.get("percent")
        if percent is None or float(percent) >= float(threshold):
            continue
        boss = row.get("boss") or "Unknown boss"
        out.append({"boss": boss, "number": numbers.get(str(boss).casefold()),
                    "name": row.get("name") or "?", "percent": round(float(percent), 1),
                    "spec": row.get("spec") or "", "class": row.get("class") or "",
                    "role": row.get("role") or ""})
    # Unnumbered bosses sort last rather than first, so a naming mismatch does not
    # reorder the whole raid.
    out.sort(key=lambda e: (e["number"] is None, e["number"] or 0, e["percent"], e["name"]))
    return out


def lines(entries):
    """The DM body: a boss heading, then its people. The heading carries the boss number,
    so no line repeats it."""
    body, current = [], object()
    for entry in entries[:MAX_LINES]:
        if entry["boss"] != current:
            current = entry["boss"]
            number = f"Boss {entry['number']} · " if entry["number"] else ""
            body.append(f"\n**{number}{entry['boss']}**")
        spec = f" ({entry['spec']})" if entry["spec"] else ""
        body.append(f"`{entry['percent']:>5.1f}%` {entry['name']}{spec}")
    if len(entries) > MAX_LINES:
        body.append(f"\n…and {len(entries) - MAX_LINES} more.")
    return "\n".join(body).strip()


def message(entries, *, team_name, difficulty, raid, night_text, threshold=25.0,
            page_url=""):
    """The Discord payload, or None when nobody was grey -- which is the good night, and
    the recap says nothing extra about it."""
    if not entries:
        return None
    people = len({e["name"] for e in entries})
    title = f"Grey parses · {night_text}" if night_text else "Grey parses"
    where = " · ".join(s for s in (team_name, difficulty, raid) if s)
    description = (f"{len(entries)} parse{'s' if len(entries) != 1 else ''} under "
                   f"{threshold:g}% from {people} raider{'s' if people != 1 else ''}."
                   f"\n{where}" if where else "")
    embed = {"title": title, "description": description + "\n" + lines(entries),
             "color": COLOR,
             "footer": {"text": "greyBot · sent only to you · rankPercent, the number raiders "
                                "mean by \"parse\""}}
    if page_url:
        embed["url"] = page_url
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}
