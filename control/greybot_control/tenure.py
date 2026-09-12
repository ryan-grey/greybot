"""Permission-free year badges; existing staff role icons always take precedence."""
import asyncio
import base64
import calendar
from pathlib import Path
from datetime import datetime, timezone

from .anniversaries import ZONE

BADGES = Path(__file__).resolve().parents[2] / 'assets' / 'tenure-badges'


def icon(years):
    """Pre-rendered Trebuchet Bold numbers; no font or renderer needed at runtime."""
    if not isinstance(years,int) or not 1 <= years <= 99:
        raise ValueError('Year badge must be between 1 and 99')
    png=(BADGES / f'{years}.png').read_bytes()
    return 'data:image/png;base64,'+base64.b64encode(png).decode()


def years(member,today):
    if member.get('user',{}).get('bot') or not member.get('joined_at'): return None
    try:
        joined=datetime.fromisoformat(member['joined_at'].replace('Z','+00:00'))
        if joined.tzinfo is None:return None
        joined=joined.astimezone(ZONE).date()
    except (ValueError,TypeError):return None
    anniversary=(joined.month,min(joined.day,calendar.monthrange(today.year,joined.month)[1]))
    return max(0,today.year-joined.year-((today.month,today.day)<anniversary))


def install(store):
    with store.connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS tenure_config(guild TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS tenure_roles(
                guild TEXT NOT NULL,years INTEGER NOT NULL,role TEXT NOT NULL,
                PRIMARY KEY(guild,years));
        ''')


async def tick(cfg,store,api,now=None):
    if not cfg.enforce:return
    with store.connection() as db:
        if not db.execute('SELECT 1 FROM tenure_config WHERE guild=?',(cfg.guild_id,)).fetchone():return
        known={r['years']:r['role'] for r in db.execute('SELECT * FROM tenure_roles WHERE guild=?',(cfg.guild_id,))}
    base=f'/guilds/{cfg.guild_id}'
    roles=await api.request('GET',base+'/roles')
    by_id={r['id']:r for r in roles}
    for rid in known.values():
        if not rid or rid not in by_id:raise RuntimeError('Year badge role requires reconciliation')
        r=by_id[rid]
        if int(r['permissions']) or r.get('color') or r.get('hoist') or r.get('mentionable'):
            raise RuntimeError('Year badge role is no longer cosmetic')
    # Any pre-existing icon remains authoritative, including GM and Officer.
    protected={r['id'] for r in roles if (r.get('icon') or r.get('unicode_emoji')) and r['id'] not in known.values()}
    today=(now or datetime.now(timezone.utc)).astimezone(ZONE).date()
    after='0'
    changed=0
    for _ in range(100):
        page=await api.request('GET',base+f'/members?limit=1000&after={after}')
        if not isinstance(page,list):raise RuntimeError('Year badge members unavailable')
        for member in page:
            held=set(member.get('roles',[]))
            age=None if held & protected else years(member,today)
            if age is not None and age not in known:
                with store.connection() as db:
                    claimed=db.execute('INSERT OR IGNORE INTO tenure_roles VALUES(?,?,?)',(cfg.guild_id,age,'')).rowcount
                if not claimed:raise RuntimeError('Year badge creation needs reconciliation')
                result=await api.request('POST',base+'/roles',body={
                    'name':f'{age} Year'+('s' if age!=1 else '')+' in Server',
                    'permissions':'0','color':0,'hoist':False,'mentionable':False,'icon':icon(age) if age else None},
                    reason='Membership anniversary badge; no channel permissions')
                known[age]=result['id']
                with store.connection() as db:
                    db.execute('UPDATE tenure_roles SET role=? WHERE guild=? AND years=?',(result['id'],cfg.guild_id,age))
            wanted=known.get(age) if age is not None else None
            uid=member['user']['id']
            # Remove only roles owned by this feature, preserving every other role.
            for rid in held & set(known.values()) - ({wanted} if wanted else set()):
                await api.request('DELETE',base+f'/members/{uid}/roles/{rid}',reason='Refresh membership year badge')
                changed+=1
                await asyncio.sleep(0.3)
            if wanted and wanted not in held:
                await api.request('PUT',base+f'/members/{uid}/roles/{wanted}',reason='Membership year badge')
                changed+=1
                await asyncio.sleep(0.3)
        if len(page)<1000:return changed
        next_after=str(max(int(m['user']['id']) for m in page))
        if int(next_after)<=int(after):raise RuntimeError('Year badge pagination stalled')
        after=next_after
    raise RuntimeError('Year badge pagination limit')
