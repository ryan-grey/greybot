"""Permission-free year badges; existing staff role icons always take precedence."""
import asyncio
import base64
import calendar
import struct
import zlib
from datetime import datetime, timezone

from .anniversaries import ZONE

DIGITS = ('111101101101111','010110010010111','111001111100111',
          '111001111001111','101101111001001','111100111001111',
          '111100111101111','111001001001001','111101111101111','111101111001111')
QUALITY_COLORS = ('9d9d9d','ffffff','1eff00','0070dd','a335ee','ff8000')


def icon(years):
    """Small lossless number icon using only the standard library."""
    chars = str(years)
    scale = min(10, 54 // (len(chars)*4-1))
    width = (len(chars)*4-1)*scale
    pixels = bytearray(64*64*4)
    color=bytes.fromhex(QUALITY_COLORS[min(years,5)])+b'\xff'
    for index,ch in enumerate(chars):
        for n,bit in enumerate(DIGITS[int(ch)]):
            if bit == '0': continue
            for dy in range(scale):
                for dx in range(scale):
                    x=(64-width)//2+(index*4+n%3)*scale+dx
                    y=(64-5*scale)//2+(n//3)*scale+dy
                    p=(y*64+x)*4
                    pixels[p:p+4]=color
    def chunk(kind,data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    raw=b''.join(b'\0'+pixels[y*256:(y+1)*256] for y in range(64))
    png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',64,64,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')
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
                    'permissions':'0','color':0,'hoist':False,'mentionable':False,'icon':icon(age)},
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
