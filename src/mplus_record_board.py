"""One persistent, pinned record card, refreshed before record announcements."""
import hashlib
import io
import json
import urllib.request

import discord
import mplus
import recap_card
import recap_page


def current_runs(state, seasons, region, now):
    active = next((s for s in seasons if mplus.stamp(s['starts'][region]) <= now
                   and (not s.get('ends', {}).get(region) or now < mplus.stamp(s['ends'][region]))), None)
    if not active:
        raise ValueError('Current season metadata required for record card')
    runs = sorted((r for r in state['best'].values() if r['season'] == active['slug']),
                  key=lambda r: r['dungeon'])
    return active, runs


def render(runs, guild, season):
    from PIL import Image, ImageDraw
    from mplus_records import timer
    c = recap_card
    pairs = [runs[i:i+2] for i in range(0, len(runs), 2)]
    heights = [100 + 22 * max(len(r['guild_members']) for r in pair) for pair in pairs]
    height = 160 + sum(h + 16 for h in heights)
    image = Image.new('RGB', (1280, height * 2), c.BG)
    canvas = c._Canvas(image, ImageDraw.Draw(image), {})
    canvas.text(24, 18, 'GREYBOT  /  GUILD RECORDS', canvas.font('bold', 12), c.ACCENT, spacing=1)
    canvas.text(24, 43, 'Mythic+ dungeon records', canvas.font('bold', 29), c.INK)
    canvas.text(24, 83, f'{guild} · {season}', canvas.font('regular', 15), c.MUTED)
    canvas.text(24, 108, 'Highest timed key · fastest tie · 2+ guild members', canvas.font('regular', 14), c.MUTED)
    y = 144
    for pair, panel_height in zip(pairs, heights):
        for column, run in enumerate(pair):
            x = 24 + column * 304
            canvas.rect(x, y, x+288, y+panel_height, fill=c.CHIP, outline=c.LINE, radius=10)
            canvas.text(x+14, y+12, c._ellipsis(canvas, run['dungeon'], canvas.font('bold', 17), 260), canvas.font('bold', 17), c.INK)
            canvas.text(x+14, y+40, f'+{run["level"]}', canvas.font('bold', 30), (63, 185, 80))
            canvas.text(x+91, y+49, timer(run['elapsed_ms']), canvas.font('semibold', 18), c.INK)
            canvas.text(x+14, y+78, 'GUILD RECORD HOLDERS', canvas.font('bold', 10), c.MUTED, spacing=0.6)
            members = [p for p in run['roster'] if p['key'] in run['guild_members']]
            for index, person in enumerate(members):
                color = recap_page.class_color(person.get('class', '')) or '#f0f6fc'
                canvas.text(x+14, y+98+index*22, person['name'], canvas.font('semibold', 16), color)
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


def sync(repo, cfg, channel, state, now):
    active, runs = current_runs(state, (repo.get('SEASONS') or {}).get('items', []), cfg['guild_region'], now)
    if not runs:
        return None
    fingerprint = hashlib.sha256(json.dumps(runs, sort_keys=True).encode()).hexdigest()[:24]
    key = 'RECORD_BOARD#' + channel
    saved = repo.get(key) or {}
    if saved and not saved.get('message'):
        raise RuntimeError('Record card creation needs review before retrying')
    message = saved.get('message')
    if saved.get('fingerprint') != fingerprint:
        image = render(runs, cfg['guild_name'], active['name'])
        from handler import publish_bytes
        path = f'mplus/records/{fingerprint}.png'
        publish_bytes(cfg, path, image, 'image/png')
        image_url = cfg['recap_page_url'].rstrip('/') + '/' + path
        body = {'allowed_mentions': {'parse': []}, 'embeds': [{
            'title': cfg['guild_name'] + ' · Mythic+ records',
            'description': 'Highest timed keys with 2+ guild members; fastest time breaks ties. This pinned card updates when records change.',
            'color': 0x4493F8, 'image': {'url': image_url},
            'author': {'name': 'Raider.IO', 'url': 'https://raider.io'},
            'timestamp': now.isoformat(), 'footer': {'text': active['name'] + ' · Observed guild records'}}]}
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
