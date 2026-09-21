"""Members nominate a post; enough nominations feature it in the featured channel.

Two ways in, one counter: a star reaction, and a "Feature this post" entry in
Discord's message menu for people who never discover reactions. A member's vote
counts once however they cast it, and removing the star takes it back until the
post is featured -- after that it stays featured, because a card that appears
and vanishes reads as a moderator deleting somebody's post.
"""

import hashlib
import time
from urllib.parse import quote

from .discord_api import Denied
from .profiles import display_profile

STAR = "⭐"
PREFIX = "greybot:feature:"
COMMAND_NAMES = {"Feature this post"}
COLOR = 0xF5C542
# Discord rejects an embed description over 4096; leave room for the ellipsis.
EXCERPT = 3800


def install(store):
    with store.connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS feature_nominations(
                guild TEXT NOT NULL, message TEXT NOT NULL, member TEXT NOT NULL,
                channel TEXT NOT NULL, source TEXT NOT NULL, at REAL NOT NULL,
                PRIMARY KEY(guild,message,member));
            CREATE TABLE IF NOT EXISTS feature_delivery(
                guild TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '', stars INTEGER NOT NULL DEFAULT 0,
                observed REAL NOT NULL, PRIMARY KEY(guild,message));
            CREATE TABLE IF NOT EXISTS feature_marks(
                guild TEXT NOT NULL, message TEXT NOT NULL, channel TEXT NOT NULL,
                state TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(guild,message));
        ''')
        # A claim that provably published nothing resumes after a restart.
        db.execute("UPDATE feature_delivery SET state='pending' WHERE state='featuring' AND target=''")


def enabled(cfg):
    return bool(cfg.enforce and cfg.featured_channel_id and cfg.feature_category_id)


def eligible(cfg, channel, parent):
    """Only the social category, and never the featured channel itself."""
    return bool(channel and channel != cfg.featured_channel_id and parent == cfg.feature_category_id)


def nominate(cfg, store, message, member, channel, source):
    """One vote per member per message, however they cast it. Returns the new count."""
    with store.connection() as db:
        db.execute('INSERT OR IGNORE INTO feature_nominations VALUES(?,?,?,?,?,?)',
                   (cfg.guild_id, message, member, channel, source, time.time()))
        stars = db.execute('SELECT COUNT(*) n FROM feature_nominations WHERE guild=? AND message=?',
                           (cfg.guild_id, message)).fetchone()['n']
        if stars >= cfg.feature_threshold:
            db.execute('INSERT OR IGNORE INTO feature_delivery(guild,message,state,stars,observed)'
                       ' VALUES(?,?,?,?,?)', (cfg.guild_id, message, 'pending', stars, time.time()))
    return stars


def mark(cfg, store, message, channel):
    """A menu nomination is invisible, so greyBot puts the first star on the post itself.

    That star is what everyone else sees and clicks: Discord's own reaction pill is
    both the counter and the button. greyBot takes its own star back as soon as a
    member has put one there, so the number on the pill is the real vote count.
    """
    with store.connection() as db:
        db.execute('INSERT INTO feature_marks(guild,message,channel,state,at) VALUES(?,?,?,?,?)'
                   ' ON CONFLICT(guild,message) DO NOTHING',
                   (cfg.guild_id, message, channel, 'pending', time.time()))


def withdraw(cfg, store, message, member):
    """Taking the star back counts, right up until the post is featured."""
    with store.connection() as db:
        featured = db.execute('SELECT 1 FROM feature_delivery WHERE guild=? AND message=?',
                              (cfg.guild_id, message)).fetchone()
        if not featured:
            db.execute('DELETE FROM feature_nominations WHERE guild=? AND message=? AND member=?',
                       (cfg.guild_id, message, member))


def observe(cfg, store, packet):
    """A star added or removed on a message in the social category."""
    if not enabled(cfg) or packet.get('t') not in {'MESSAGE_REACTION_ADD', 'MESSAGE_REACTION_REMOVE'}:
        return
    data = packet.get('d') or {}
    emoji = data.get('emoji') or {}
    if (str(data.get('guild_id') or '') != cfg.guild_id or emoji.get('id')
            or emoji.get('name') != STAR or not data.get('message_id') or not data.get('user_id')):
        return
    if (data.get('member') or {}).get('user', {}).get('bot'):
        return
    message, member = str(data['message_id']), str(data['user_id'])
    channel = str(data.get('channel_id') or '')
    if packet['t'] == 'MESSAGE_REACTION_REMOVE':
        withdraw(cfg, store, message, member)
        return
    # A member has starred it, so greyBot's seed star has done its job and should
    # come off; the pill stays up on the member's own star.
    with store.connection() as db:
        db.execute("UPDATE feature_marks SET state='clearing' WHERE guild=? AND message=?"
                   " AND state='seeded'", (cfg.guild_id, message))
    # The reaction payload has no parent, so the channel is checked when the
    # card is built. A nomination in an ineligible channel simply never features.
    nominate(cfg, store, message, member, channel, 'reaction')


def receive(cfg, store, packet):
    """Answer the message-menu command directly: the vote is a database write, not a Discord call."""
    member = packet.get('member', {})
    actor = member.get('user', {}).get('id')
    if (packet.get('guild_id') != cfg.guild_id or packet.get('application_id') != cfg.client_id
            or not actor or member.get('user', {}).get('bot') or member.get('pending')):
        raise Denied('A valid server interaction is required')
    if not enabled(cfg):
        raise Denied('Featuring is not switched on in this server.')
    data = packet.get('data', {})
    target = str(data.get('target_id') or '')
    if not target.isdecimal():
        raise Denied('Pick a message to feature.')
    channel = packet.get('channel') or {}
    if not eligible(cfg, str(channel.get('id') or ''), str(channel.get('parent_id') or '')):
        raise Denied(f'Posts can only be featured from the social channels, not <#{channel.get("id")}>.')
    resolved = (data.get('resolved') or {}).get('messages', {}).get(target, {})
    if resolved.get('author', {}).get('id') == cfg.client_id:
        raise Denied('That one is already one of mine.')
    with store.connection() as db:
        already = db.execute('SELECT 1 FROM feature_delivery WHERE guild=? AND message=?',
                             (cfg.guild_id, target)).fetchone()
    if already:
        return reply(f'That post is already on its way to <#{cfg.featured_channel_id}>.')
    stars = nominate(cfg, store, target, str(actor), str(channel.get('id') or ''), 'menu')
    mark(cfg, store, target, str(channel.get('id') or ''))
    short = cfg.feature_threshold - stars
    if short > 0:
        return reply(f'{STAR} Noted -- that post has **{stars}** of **{cfg.feature_threshold}** '
                     f'nominations. {short} more and it lands in <#{cfg.featured_channel_id}>. '
                     f'Everyone else can just click the {STAR} on the post itself.')
    return reply(f'{STAR} That is **{stars}** nominations -- it is going to '
                 f'<#{cfg.featured_channel_id}> now.')


def reply(content):
    return {'type': 4, 'data': {'content': content, 'flags': 64, 'allowed_mentions': {'parse': []}}}


def excerpt(text):
    text = text or ''
    return text if len(text) <= EXCERPT else text[:EXCERPT].rstrip() + ' ...'


def picture(message):
    """The first image the post carries, so a memes card looks like the meme."""
    for attachment in message.get('attachments') or []:
        if str(attachment.get('content_type') or '').startswith('image/') and attachment.get('url'):
            return attachment['url']
    for embed in message.get('embeds') or []:
        for key in ('image', 'thumbnail'):
            if (embed.get(key) or {}).get('url'):
                return embed[key]['url']
    return ''


def card(guild, message, channel, stars, link):
    author = message.get('author') or {}
    profile = (display_profile(guild, author, message.get('member') or {}) if author.get('id')
               else {'name': 'Unknown member', 'avatar_url': 'https://cdn.discordapp.com/embed/avatars/0.png'})
    embed = {'author': {'name': profile['name'], 'icon_url': profile['avatar_url']},
             'description': excerpt(message.get('content')),
             'color': COLOR,
             'fields': [{'name': 'Original', 'value': f'[Jump to the post]({link})', 'inline': False}],
             'footer': {'text': f'{STAR} {stars} · #{channel}'},
             'timestamp': message.get('timestamp')}
    image = picture(message)
    if image:
        embed['image'] = {'url': image}
    if not embed['description'] and not image:
        embed['description'] = '*(no text -- open the original)*'
    return {'content': '', 'embeds': [embed], 'allowed_mentions': {'parse': []}}


def own_star(message):
    """Whether greyBot's own star is on the post, and whether anyone's is."""
    for reaction in message.get('reactions') or []:
        emoji = reaction.get('emoji') or {}
        if not emoji.get('id') and emoji.get('name') == STAR:
            return bool(reaction.get('me')), int(reaction.get('count') or 0)
    return False, 0


async def marks_tick(cfg, store, api):
    """Put greyBot's seed star on one post, or take one back. Both calls are idempotent."""
    if not enabled(cfg):
        return
    with store.connection() as db:
        row = db.execute("SELECT message,channel,state FROM feature_marks WHERE guild=?"
                         " AND state IN ('pending','clearing') ORDER BY at LIMIT 1",
                         (cfg.guild_id,)).fetchone()
    if not row:
        return
    message, channel, state = row['message'], row['channel'], row['state']
    reaction = f'/channels/{channel}/messages/{message}/reactions/{quote(STAR)}/@me'

    def settle(new):
        with store.connection() as db:
            db.execute('UPDATE feature_marks SET state=? WHERE guild=? AND message=?',
                       (new, cfg.guild_id, message))

    if state == 'clearing':
        try:
            await api.request('DELETE', reaction)
        except Denied:
            pass  # Already gone, or the post is.
        settle('done')
        return
    try:
        original = await api.request('GET', f'/channels/{channel}/messages/{message}')
    except Denied:
        settle('gone')
        return
    mine, count = own_star(original or {})
    if count and not mine:
        settle('done')  # A member's star is already the pill; no seed needed.
        return
    if not mine:
        try:
            await api.request('PUT', reaction)
        except Denied:
            settle('gone')
            return
    settle('seeded')


async def tick(cfg, store, api):
    """Feature one pending post. Every state settles once and nothing is retried."""
    if not enabled(cfg):
        return
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT message,stars FROM feature_delivery WHERE guild=? AND state='pending'"
                         ' ORDER BY observed LIMIT 1', (cfg.guild_id,)).fetchone()
        if row:
            db.execute("UPDATE feature_delivery SET state='featuring' WHERE guild=? AND message=?",
                       (cfg.guild_id, row['message']))
    if not row:
        return
    message, stars = row['message'], row['stars']

    def settle(state, target=''):
        with store.connection() as db:
            db.execute('UPDATE feature_delivery SET state=?,target=? WHERE guild=? AND message=?',
                       (state, target, cfg.guild_id, message))

    with store.connection() as db:
        source = db.execute("SELECT channel FROM feature_nominations WHERE guild=? AND message=?"
                            " AND channel!='' LIMIT 1", (cfg.guild_id, message)).fetchone()
    if not source:
        settle('skipped')
        return
    channel = source['channel']
    try:
        info = await api.request('GET', f'/channels/{channel}')
        original = await api.request('GET', f'/channels/{channel}/messages/{message}')
    except Denied:
        settle('gone')  # Deleted, or greyBot cannot read that channel any more.
        return
    except Exception:
        settle('unknown')
        raise
    if not eligible(cfg, channel, str((info or {}).get('parent_id') or '')):
        settle('ineligible')  # Nominated somewhere featuring does not apply.
        return
    link = f'https://discord.com/channels/{cfg.guild_id}/{channel}/{message}'
    nonce = hashlib.sha256(f'feature:{cfg.guild_id}:{message}'.encode()).hexdigest()[:24]
    try:
        posted = await api.request('POST', f'/channels/{cfg.featured_channel_id}/messages',
                                   body={**card(cfg.guild_id, original, (info or {}).get('name', ''), stars, link),
                                         'nonce': nonce, 'enforce_nonce': True})
    except Exception:
        settle('unknown')
        raise
    settle('featured', posted['id'])
