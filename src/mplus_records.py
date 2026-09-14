"""Seasonal dungeon records with a quiet baseline and at-most-once delivery."""
import time
import hashlib
import os
from urllib.parse import urlsplit
from functools import lru_cache

import discord
import mplus
import mplus_role_icons
from mplus_presentation import safe_url

QUEUE='RECORD_CANDIDATE#'

@lru_cache(maxsize=4)
def dungeon_artwork(season):
    from mplus_collect import fetch
    data = fetch('mythic-plus/static-data', expansion_id=int(os.environ.get('MPLUS_EXPANSION_ID', '11')))
    current = next(s for s in data['seasons'] if s['slug'] == season)
    return {d['name']: d['background_image_url'] for d in current['dungeons']}


def record_key(run):
    return run['season']+'/'+run['dungeon'].casefold()


def improves(run, previous):
    return (run['timed'] and len(set(run['guild_members'])) >= 2
            and (not previous or run['level'] > previous['level']
                 or (run['level'] == previous['level'] and run['elapsed_ms'] < previous['elapsed_ms'])))


def timer(milliseconds):
    seconds,ms=divmod(milliseconds,1000)
    minutes,seconds=divmod(seconds,60)
    return f'{minutes}:{seconds:02d}.{ms:03d}'


def payload(run, previous=None, board_url=None, board_image=None, dungeon_art=None):
    def clean(value):
        value=str(value).replace('@','＠')
        for char in ('\\','*','_','`','~','|','[',']'):
            value=value.replace(char,'\\'+char)
        return value
    def member(p):
        return (mplus_role_icons.EMOJI.get(p.get('role') or mplus.class_role(p.get('class')),'')+' '+clean(p['name'])).strip()
    members=', '.join(member(p) for p in run['roster'] if p['key'] in run['guild_members'])
    description=f'New Record Set by **{members}**\n**{clean(run["dungeon"])} +{run["level"]}** · **{timer(run["elapsed_ms"])}**'
    fields=[{'name':'Guild members','value':f'{len(run["guild_members"])}/5','inline':True},
            {'name':'Time limit','value':timer(run['timer_ms']),'inline':True}]
    if previous:
        fields.append({'name':'Previous record','value':f'+{previous["level"]} · {timer(previous["elapsed_ms"])}','inline':True})
        old_members=', '.join(member(p) for p in previous['roster'] if p['key'] in previous['guild_members'])
        fields.append({'name':'Previous guild record holders','value':old_members or 'Unavailable','inline':False})
    embed={'title':'🏆 New Record Set','description':description,'fields':fields,'color':0x4493F8,
           'timestamp':run['completed'],'footer':{'text':'greyBot · Guild Mythic+ record'}}
    url=safe_url(run['url'])
    result={'embeds':[embed],'allowed_mentions':{'parse':[]},
            'nonce':hashlib.sha256(('mplus-record:'+run['id']).encode()).hexdigest()[:24],'enforce_nonce':True}
    if url:
        embed['url']=url
        result['components']=[{'type':1,'components':[{'type':2,'style':5,'label':'View run','url':url}]}]
    if board_url:
        result['content'] = f'🏆 [Updated dungeon-record card]({board_url})'
        result.setdefault('components', [{'type':1,'components':[]}])[0]['components'].insert(0,
            {'type':2,'style':5,'label':'Pinned record card','url':board_url})
    if board_image:
        image_url = urlsplit(board_image)
        if image_url.scheme == 'https' and image_url.hostname and not image_url.username and not image_url.password:
            embed['image'] = {'url': board_image}
    if dungeon_art:
        parsed = urlsplit(dungeon_art)
        if parsed.scheme == 'https' and parsed.netloc == 'cdn.raiderio.net' and parsed.path.startswith('/images/dungeons/'):
            embed['title'] = f'🏆 New Record Set · {run["dungeon"]} +{run["level"]}'
            if embed.get('image'):
                result['embeds'].append({'title':'Updated season leaderboard', 'url':board_url,
                                         'color':0x4493F8, 'image':embed['image']})
            embed['image'] = {'url':dungeon_art}
    return result


def process(repo, cfg, channel, now, budget=35, post=discord.post_to):
    owner=repo.lease('RECORD_LEASE')
    if not owner:return {'skipped':'records_busy'}
    started=time.monotonic()
    try:
        state=repo.get('RECORDS')
        if not state:
            collector=repo.get('COLLECTOR') or {}
            if not collector.get('roster_size') or collector.get('record_baseline_profiles',0) < collector['roster_size']:
                return {'skipped':'warming_record_baseline'}
            best={}
            for run in repo.prefix('RUN#'):
                key=record_key(run)
                if improves(run,best.get(key)):best[key]=run
            state={'best':best,'since':now.isoformat(),'cursor':QUEUE+now.isoformat()+'#'}
            repo.put('RECORDS',state)
            return {'baseline_ready':True,'dungeon_records':len(best)}
        count=0
        board_url=None
        if os.environ.get('MPLUS_RECORD_BOARD_ENABLED') == '1':
            from mplus_record_board import sync
            board_url=sync(repo,cfg,channel,state,now)
        for cursor,run in repo.after(QUEUE,state['cursor']):
            if time.monotonic()-started >= budget:break
            key=record_key(run)
            previous=state['best'].get(key)
            better=improves(run,previous)
            # Historical discovery can improve the baseline without sending old news.
            announce=better and mplus.stamp(run['completed']) > mplus.stamp(state['since'])
            claim='RECORD_POST#'+run['id']
            if better and os.environ.get('MPLUS_RECORD_BOARD_ENABLED') == '1':
                updated={**state,'best':{**state['best'],key:run}}
                board_url=sync(repo,cfg,channel,updated,now)
            art = dungeon_artwork(run['season']).get(run['dungeon']) if announce else None
            if announce and repo.put(claim,{'state':'sending','at':now.isoformat(),'run':run['id']},once=True):
                try:
                    board = repo.get('RECORD_BOARD#' + channel) or {}
                    result=post({'bot_token':cfg['bot_token'],'channel':channel},payload(run,previous,board_url,board.get('image') if board_url else None,art),timeout=8,max_attempts=1)
                    repo.put(claim,{'state':'posted','at':now.isoformat(),'run':run['id'],
                                   'message':str(getattr(result,'message_id',''))})
                    count+=1
                except Exception:
                    # Acceptance may precede a network timeout. Never resend automatically.
                    repo.put(claim,{'state':'needs_review','at':now.isoformat(),'run':run['id']})
                    state['best'][key]=run
                    state['cursor']=cursor
                    repo.put('RECORDS',state)
                    raise
            if better:state['best'][key]=run
            state['cursor']=cursor
            repo.put('RECORDS',state)
        return {'record_alerts':count,'dungeon_records':len(state['best'])}
    finally:
        repo.release(owner,'RECORD_LEASE')
