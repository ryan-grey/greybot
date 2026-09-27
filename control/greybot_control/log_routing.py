"""Warcraft Logs posts outside progression raid hours belong in Saturday logs.

The Warcraft Logs integration posts every report its guild sees into the
progression team's log channel, including the ones a progression raider starts
on a Saturday night for the other team. Those are republished in the Saturday
team's channel and then removed from the progression one -- in that order, so
an interrupted move leaves the report visible twice rather than nowhere.
"""

import hashlib
import re
import time
from datetime import datetime, time as clock, timedelta
from zoneinfo import ZoneInfo

from .discord_api import Denied

ZONE = ZoneInfo('America/New_York')

# Use the post's timestamp: progression raids Tuesday and Thursday,
# 9pm to midnight Eastern, with one hour of leeway on either side.
PROG_NIGHTS = (1, 3)  # Monday is 0.
PROG_START = clock(21)
PROG_HOURS = 3
PROG_LEEWAY_HOURS = 1

REPORT = re.compile(r'https?://(?:www\.)?warcraftlogs\.com/reports/[A-Za-z0-9]+')


def install(store):
    with store.connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS log_route_delivery(
                guild TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '', observed REAL NOT NULL,
                PRIMARY KEY(guild,message));
        ''')
        # A claim interrupted before anything published resumes: the publish
        # itself looks for the report in the target channel before writing.
        db.execute("UPDATE log_route_delivery SET state='pending' WHERE state='moving' AND target=''")


def prog_night(when):
    """True while a report still counts as the progression team's raid night."""
    when = when.astimezone(ZONE)
    for days in (0, 1):
        day = (when - timedelta(days=days)).date()
        if day.weekday() not in PROG_NIGHTS:
            continue
        start = datetime.combine(day, PROG_START, ZONE)
        if (start - timedelta(hours=PROG_LEEWAY_HOURS)
                <= when < start + timedelta(hours=PROG_HOURS + PROG_LEEWAY_HOURS)):
            return True
    return False


def misrouted(when):
    """Any report posted outside Tuesday/Thursday progression raid hours."""
    return not prog_night(when)


def report_link(message):
    """The report URL, wherever the integration put it: text, title, or embed."""
    for text in (message.get('content') or '',
                 *(str(value) for embed in (message.get('embeds') or [])
                   for value in (embed.get('url'), embed.get('title'), embed.get('description'))
                   if value)):
        found = REPORT.search(text)
        if found:
            return found.group(0)
    return ''


def republish(message, link):
    """Carry the integration's own embed across; a bare link loses the title."""
    embeds = []
    for embed in (message.get('embeds') or [])[:10]:
        # Discord rejects the provider/video fields it generates itself.
        embed = {key: value for key, value in embed.items()
                 if key not in {'type', 'provider', 'video', 'timestamp'}}
        embeds.append(embed)
    if embeds:
        embeds[0]['footer'] = {'text': 'greyBot · moved from the progression log channel'}
    return {'content': message.get('content') or ('' if embeds else link),
            'embeds': embeds, 'allowed_mentions': {'parse': []}}


def observe(cfg, store, packet):
    """Record a post outside progression hours, from a member, bot, or webhook."""
    if not routing_enabled(cfg) or packet.get('t') != 'MESSAGE_CREATE':
        return
    data = packet.get('d') or {}
    # tick checks for a report link before publishing or deleting anything.
    if (str(data.get('guild_id') or '') != cfg.guild_id
            or str(data.get('channel_id') or '') != cfg.prog_logs_channel_id
            or not data.get('id')):
        return
    try:
        when = datetime.fromisoformat(str(data['timestamp'])).astimezone(ZONE)
    except (KeyError, TypeError, ValueError):
        return
    if not misrouted(when):
        return
    with store.connection() as db:
        db.execute('INSERT OR IGNORE INTO log_route_delivery(guild,message,state,observed) VALUES(?,?,?,?)',
                   (cfg.guild_id, str(data['id']), 'pending', time.time()))


def routing_enabled(cfg):
    return bool(cfg.enforce and cfg.prog_logs_channel_id and cfg.sat_logs_channel_id)


async def tick(cfg, store, api):
    """Move one recorded report. States settle once; nothing is ever retried."""
    if not routing_enabled(cfg):
        return
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT message FROM log_route_delivery WHERE guild=? AND state='pending'"
                         ' ORDER BY observed LIMIT 1', (cfg.guild_id,)).fetchone()
        if row:
            db.execute("UPDATE log_route_delivery SET state='moving' WHERE guild=? AND message=?",
                       (cfg.guild_id, row['message']))
    if not row:
        return
    message = row['message']

    def settle(state, target=''):
        with store.connection() as db:
            db.execute('UPDATE log_route_delivery SET state=?,target=? WHERE guild=? AND message=?',
                       (state, target, cfg.guild_id, message))

    try:
        original = await api.request('GET', f'/channels/{cfg.prog_logs_channel_id}/messages/{message}')
    except Denied:
        settle('gone')  # Deleted by hand, or the channel is no longer readable.
        return
    except Exception:
        settle('unknown')
        raise
    link = report_link(original or {})
    if not link:
        settle('skipped')  # An integration post about something other than a report.
        return
    try:
        recent = await api.request('GET', f'/channels/{cfg.sat_logs_channel_id}/messages?limit=50')
    except Exception:
        settle('unknown')
        raise
    # An interrupted move, or a raider who already pasted the link, means the
    # Saturday channel has the report: publish nothing and remove the original.
    target = next((str(post['id']) for post in (recent or []) if report_link(post) == link), '')
    if not target:
        nonce = hashlib.sha256(f'log-route:{cfg.guild_id}:{message}'.encode()).hexdigest()[:24]
        try:
            posted = await api.request('POST', f'/channels/{cfg.sat_logs_channel_id}/messages',
                                       body={**republish(original, link), 'nonce': nonce, 'enforce_nonce': True})
        except Exception:
            settle('unknown')
            raise
        target = str(posted['id'])
    settle('published', target)
    try:
        await api.request('DELETE', f'/channels/{cfg.prog_logs_channel_id}/messages/{message}',
                          reason='Report posted outside progression raid hours moved to Saturday logs')
    except Exception:
        # The report is safe in both channels; removing the original is a manual cleanup.
        settle('duplicated', target)
        raise
    settle('moved', target)
