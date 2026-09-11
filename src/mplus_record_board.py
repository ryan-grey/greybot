"""One persistent, pinned record card, refreshed before record announcements."""
import hashlib
import io
import json
import html
import urllib.request
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor

import discord
import mplus
import recap_card
import recap_page

STYLE_VERSION = 'season-title-v5'
_art_cache = {}


def board_title(season, emoji=True):
    name=season.replace('MN Season','Midnight Season').split(' • ')[0]
    return ('🗝️ ' if emoji else '') + name + '’s Highest Timed M+ Keys' + (' 🗝️' if emoji else '')


def artwork(season, runs):
    """Read official dungeon artwork URLs from Raider.IO, with bounded downloads."""
    from PIL import Image
    from mplus_collect import fetch
    if season not in _art_cache or any(r['dungeon'] not in _art_cache[season] for r in runs):
        import os
        metadata = fetch('mythic-plus/static-data', expansion_id=int(os.environ.get('MPLUS_EXPANSION_ID', '11')))
        current = next(s for s in metadata['seasons'] if s['slug'] == season)
        wanted = {r['dungeon'] for r in runs}
        def load(dungeon):
            url = dungeon['background_image_url']
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.netloc != 'cdn.raiderio.net' or not parsed.path.startswith('/images/dungeons/'):
                raise ValueError('Untrusted dungeon artwork URL')
            req = urllib.request.Request(url, headers={'User-Agent': 'greyBot'})
            with urllib.request.urlopen(req, timeout=6) as response:
                if urlsplit(response.url).netloc != 'cdn.raiderio.net':
                    raise ValueError('Unexpected artwork redirect')
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError('Dungeon artwork exceeds size limit')
            image = Image.open(io.BytesIO(data))
            if image.width * image.height > 8_000_000:
                raise ValueError('Dungeon artwork exceeds pixel limit')
            return dungeon['name'], image.convert('RGB')
        with ThreadPoolExecutor(max_workers=4) as pool:
            _art_cache[season] = dict(pool.map(load, [d for d in current['dungeons'] if d['name'] in wanted]))
    if any(r['dungeon'] not in _art_cache[season] for r in runs):
        del _art_cache[season]
        raise ValueError('Missing dungeon artwork in season metadata')
    return _art_cache[season]


def current_runs(state, seasons, region, now):
    active = next((s for s in seasons if mplus.stamp(s['starts'][region]) <= now
                   and (not s.get('ends', {}).get(region) or now < mplus.stamp(s['ends'][region]))), None)
    if not active:
        raise ValueError('Current season metadata required for record card')
    runs = sorted((r for r in state['best'].values() if r['season'] == active['slug']),
                  key=lambda r: r['dungeon'])
    return active, runs


def render(runs, guild, season, art=None):
    from PIL import Image, ImageDraw, ImageOps
    from mplus_records import timer
    c = recap_card
    pairs = [runs[i:i+2] for i in range(0, len(runs), 2)]
    heights = [134 + 22 * max(len(r['guild_members']) for r in pair) for pair in pairs]
    height = 160 + sum(h + 16 for h in heights)
    image = Image.new('RGB', (1280, height * 2), c.BG)
    canvas = c._Canvas(image, ImageDraw.Draw(image), {})
    canvas.text(24, 18, 'GREYBOT  /  GUILD RECORDS', canvas.font('bold', 12), c.ACCENT, spacing=1)
    title=board_title(season,emoji=False)
    size=24
    while canvas.width(title,canvas.font('bold',size)) > 524:size-=1
    # Draw a gold key: the bundled UI fonts do not contain colored emoji.
    canvas.draw.ellipse((48,94,74,120),outline='#e8b44a',width=5)
    canvas.draw.line((69,116,90,137),fill='#e8b44a',width=5)
    canvas.draw.line((80,125,86,119),fill='#e8b44a',width=5)
    canvas.draw.line((86,131,92,125),fill='#e8b44a',width=5)
    key_x=int((54+canvas.width(title,canvas.font('bold',size))+12)*2)
    canvas.draw.ellipse((key_x,94,key_x+26,120),outline='#e8b44a',width=5)
    canvas.draw.line((key_x+21,116,key_x+42,137),fill='#e8b44a',width=5)
    canvas.draw.line((key_x+32,125,key_x+38,119),fill='#e8b44a',width=5)
    canvas.draw.line((key_x+38,131,key_x+44,125),fill='#e8b44a',width=5)
    canvas.text(54, 43, title, canvas.font('bold', size), c.INK)
    canvas.text(24, 83, f'{guild} · Season-long leaderboard', canvas.font('regular', 15), c.MUTED)
    canvas.text(24, 108, 'Highest timed key · fastest tie · 2+ guild members', canvas.font('regular', 14), c.MUTED)
    y = 144
    for pair, panel_height in zip(pairs, heights):
        for column, run in enumerate(pair):
            x = 24 + column * 304
            canvas.rect(x, y, x+288, y+panel_height, fill=c.CHIP, outline=c.LINE, radius=10)
            if art and run['dungeon'] in art:
                tile = ImageOps.fit(art[run['dungeon']], (160, 180), method=Image.Resampling.LANCZOS)
                mask = Image.new('L', tile.size)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, 159, 179), radius=12, fill=255)
                image.paste(tile, (int((x+14)*2), int((y+14)*2)), mask)
            title_font = canvas.font('bold', 16)
            lines = ['']
            for word in run['dungeon'].split():
                candidate = (lines[-1]+' '+word).strip()
                if canvas.width(candidate, title_font) > 166 and lines[-1]:lines.append(word)
                else:lines[-1] = candidate
            for index, line in enumerate(lines[:2]):
                canvas.text(x+106, y+12+index*20, line, title_font, c.INK)
            canvas.text(x+106, y+53, f'+{run["level"]}', canvas.font('bold', 27), (63, 185, 80))
            canvas.text(x+106, y+85, timer(run['elapsed_ms']), canvas.font('semibold', 16), c.INK)
            canvas.text(x+14, y+112, 'GUILD RECORD HOLDERS', canvas.font('bold', 10), c.MUTED, spacing=0.6)
            members = [p for p in run['roster'] if p['key'] in run['guild_members']]
            for index, person in enumerate(members):
                color = recap_page.class_color(person.get('class', '')) or '#f0f6fc'
                canvas.text(x+14, y+132+index*22, person['name'], canvas.font('semibold', 16), color)
        y += panel_height + 16
    canvas.text(24, y, 'Observed season records · Raider.IO · Names use WoW class colors', canvas.font('regular', 11), c.MUTED)
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def request(token, method, path, body=None):
    req = urllib.request.Request(discord.CHANNEL_API + '/' + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': 'Bot '+token, 'Content-Type': 'application/json', 'User-Agent': 'greyBot'})
    with urllib.request.urlopen(req, timeout=8) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def page(runs, guild, season, image_url, now):
    from mplus_records import timer
    from mplus_presentation import safe_url
    esc=html.escape
    panels=[]
    for run in runs:
        members=[]
        for person in run['roster']:
            if person['key'] in run['guild_members']:
                color=recap_page.class_color(person.get('class','')) or '#f0f6fc'
                light=recap_page.class_color_on_light(color)
                members.append(f'<span class="cls" style="--c-dark:{color};--c-light:{light}">{esc(person["name"])}</span>')
        url=safe_url(run['url'])
        title=esc(run['dungeon'])
        if url:title=f'<a href="{esc(url,quote=True)}">{title}</a>'
        panels.append(f'<article><h2>{title}</h2><p class="result"><strong>+{run["level"]}</strong> {timer(run["elapsed_ms"])}</p>'
                      f'<p class="holders">{" · ".join(members)}</p><p class="note">{len(run["guild_members"])}/5 guild members · '
                      f'Time limit {timer(run["timer_ms"])}</p></article>')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60">
<title>{esc(board_title(season))}</title><style>{recap_page.STYLE}
.record-card{{display:block;width:100%;max-width:640px;height:auto;margin:24px auto;border-radius:12px}}
.records{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}}
.records article{{background:var(--surface,var(--bg));border:1px solid var(--line,#3d444d);border-radius:10px;padding:20px}}
.records h2{{font-size:19px;margin:0 0 12px}}.result{{font-size:24px}}.result strong{{color:#3fb950;margin-right:12px}}
.holders{{font-weight:600}}.records a{{color:inherit}}
</style></head><body><header class="topbar"><a class="tb-brand" href="https://ryangrey.dev">ryangrey.dev</a><span>greyBot</span></header>
<main class="wrap"><p class="kicker">{esc(guild)} · SEASON-LONG LEADERBOARD</p><h1>{esc(board_title(season))}</h1>
<p class="lede">{esc(season)} · Highest timed keys with at least two guild members; fastest time breaks ties.</p>
<p class="note">Updates automatically when greyBot finds a new record; this page refreshes every minute.</p>
<img class="record-card" src="{esc(image_url,quote=True)}" alt="Dungeon artwork and current record card; accessible results and run links follow below.">
<div class="records">{"".join(panels)}</div><p class="note">Observed season records from <a href="https://raider.io">Raider.IO</a>; source coverage and updates can lag.
Last changed <time datetime="{now.isoformat()}">{now:%Y-%m-%d %H:%M UTC}</time>.</p></main>
<footer>greyBot · Guild Mythic+ records</footer></body></html>'''


def sync(repo, cfg, channel, state, now):
    active, runs = current_runs(state, (repo.get('SEASONS') or {}).get('items', []), cfg['guild_region'], now)
    if not runs:
        return None
    fingerprint = hashlib.sha256((STYLE_VERSION+json.dumps(runs, sort_keys=True)).encode()).hexdigest()[:24]
    key = 'RECORD_BOARD#' + channel
    saved = repo.get(key) or {}
    if saved and not saved.get('message'):
        raise RuntimeError('Record card creation needs review before retrying')
    message = saved.get('message')
    if saved.get('fingerprint') != fingerprint:
        image = render(runs, cfg['guild_name'], active['name'], artwork(active['slug'], runs))
        from handler import publish_bytes
        path = f'mplus/records/{fingerprint}.png'
        publish_bytes(cfg, path, image, 'image/png')
        image_url = cfg['recap_page_url'].rstrip('/') + '/' + path
        page_url = cfg['recap_page_url'].rstrip('/') + '/mplus/records/'
        publish_bytes(cfg,'mplus/records/index.html',page(runs,cfg['guild_name'],active['name'],image_url,now).encode(),
                      'text/html; charset=utf-8',cache='public, max-age=60')
        body = {'allowed_mentions': {'parse': []}, 'embeds': [{
            'title': board_title(active['name']),
            'description': 'Highest timed keys with 2+ guild members; fastest time breaks ties. This pinned card updates when records change.',
            'color': 0x4493F8, 'image': {'url': image_url},
            'author': {'name': 'Raider.IO', 'url': 'https://raider.io'},
            'timestamp': now.isoformat(), 'footer': {'text': active['name'] + ' · Observed guild records'}}]}
        body['embeds'][0]['url']=page_url
        body['components']=[{'type':1,'components':[{'type':2,'style':5,'label':'Live dungeon records','url':page_url}]}]
        if message:
            request(cfg['bot_token'], 'PATCH', f'{channel}/messages/{message}', body)
        else:
            if not repo.put(key, {'state': 'sending', 'at': now.isoformat()}, once=True):
                raise RuntimeError('Record card already claimed')
            body.update(nonce=hashlib.sha256(('record-board:'+channel).encode()).hexdigest()[:24], enforce_nonce=True)
            result = discord.post_to({'bot_token': cfg['bot_token'], 'channel': channel}, body, max_attempts=1)
            message = str(result.message_id)
        saved = {'message': message, 'fingerprint': fingerprint, 'image': image_url, 'at': now.isoformat(), 'pinned': saved.get('pinned', False)}
        repo.put(key, saved)
    if not saved.get('pinned'):
        request(cfg['bot_token'], 'PUT', f'{channel}/messages/pins/{message}')
        saved['pinned'] = True
        repo.put(key, saved)
    return f'https://discord.com/channels/{cfg["discord_guild_id"]}/{channel}/{message}'
