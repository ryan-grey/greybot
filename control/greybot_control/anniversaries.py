"""Annual membership celebrations, with durable at-most-once delivery."""
import calendar
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .profiles import display_profile

ZONE = ZoneInfo('America/New_York')


def install(store):
    with store.connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS anniversary_config(
                guild TEXT PRIMARY KEY, channel TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS anniversary_delivery(
                guild TEXT NOT NULL, member TEXT NOT NULL, year INTEGER NOT NULL,
                state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(guild,member,year));
        ''')


def years_due(member, today):
    if member.get('user', {}).get('bot') or not member.get('joined_at'):
        return 0
    try:
        joined = datetime.fromisoformat(member['joined_at'].replace('Z','+00:00'))
        if joined.tzinfo is None:
            return 0
        joined = joined.astimezone(ZONE).date()
    except (ValueError, TypeError):
        return 0
    day = min(joined.day, calendar.monthrange(today.year, joined.month)[1])
    years = today.year - joined.year
    return years if years > 0 and (today.month,today.day)==(joined.month,day) else 0


def post(guild, member, years):
    profile = display_profile(guild, member['user'], member)
    unit = 'year' if years == 1 else 'years'
    return {
        'content': f"🎉 Happy server anniversary, <@{profile['id']}>!",
        'allowed_mentions': {'parse': [], 'users': [profile['id']]},
        'embeds': [{'author': {'name': profile['name'], 'icon_url': profile['avatar_url']},
                    'title': f'🎂 {years} {unit} in the guild!',
                    'description': f'Thanks for being part of our community for {years} {unit}!',
                    'color': 0x4493F8, 'thumbnail': {'url': profile['avatar_url']},
                    'footer': {'text': 'greyBot · Membership anniversary'}}],
    }


async def tick(cfg, store, api, now=None):
    if not cfg.enforce:
        return
    now = (now or datetime.now(timezone.utc)).astimezone(ZONE)
    if now.hour < 10:
        return
    with store.connection() as db:
        config = db.execute('SELECT channel FROM anniversary_config WHERE guild=?', (cfg.guild_id,)).fetchone()
    if not config:
        return
    # Live membership avoids congratulating departed members or stale profiles.
    after = '0'
    for _ in range(100):
        page = await api.request('GET', f'/guilds/{cfg.guild_id}/members?limit=1000&after={after}')
        if not isinstance(page, list):
            raise RuntimeError('Anniversary member list unavailable')
        for member in page:
            years = years_due(member, now.date())
            if not years:
                continue
            uid = str(member['user']['id'])
            with store.connection() as db:
                claimed = db.execute('INSERT OR IGNORE INTO anniversary_delivery(guild,member,year,state) VALUES(?,?,?,?)',
                    (cfg.guild_id,uid,now.year,'sending')).rowcount
            if not claimed:
                continue
            nonce = hashlib.sha256(f'anniversary:{cfg.guild_id}:{uid}:{now.year}'.encode()).hexdigest()[:24]
            try:
                result = await api.request('POST', f"/channels/{config['channel']}/messages",
                    body={**post(cfg.guild_id, member, years), 'nonce': nonce, 'enforce_nonce': True})
            except Exception:
                # An uncertain Discord write must never create a duplicate on restart.
                with store.connection() as db:
                    db.execute('UPDATE anniversary_delivery SET state=? WHERE guild=? AND member=? AND year=?',
                               ('unknown',cfg.guild_id,uid,now.year))
                raise
            with store.connection() as db:
                db.execute('UPDATE anniversary_delivery SET state=?,message=? WHERE guild=? AND member=? AND year=?',
                           ('published',result['id'],cfg.guild_id,uid,now.year))
        if len(page) < 1000:
            return
        next_after = str(max(int(m['user']['id']) for m in page))
        if int(next_after) <= int(after):
            raise RuntimeError('Anniversary member pagination stalled')
        after = next_after
    raise RuntimeError('Anniversary member pagination exceeded limit')
