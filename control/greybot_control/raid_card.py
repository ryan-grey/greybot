"""Signup roster image using the raid recap's fonts, palette and class colors."""
import io
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageDraw
import httpx
import recap_card as style
import recap_page
from . import raids
from .raid_emojis import EMOJIS

ROLE_IDS = {'Tank':'878310168289505301','Healer':'898011741735235645',
            'Melee':'734439523328720913','Ranged':'592446395596931072'}
SPEC_CLASSES = {
    'Warrior': ('637564444834136065','637564445031399474','637564445215948810'),
    'Death Knight': ('637564101274632192','637564101262049280','637564101329027082'),
    'Demon Hunter': ('936357056997367848','936357056649244745','1460795775557570790'),
    'Monk': ('637564262167871489','637564262054625281','637564262289637433'),
    'Druid': ('637564171696734209','637564172061900820','637564171994529798','637564172007112723'),
    'Paladin': ('637564297647489034','637564297953673216','637564297622454272'),
    'Rogue': ('637564351707873324','794267634497486858','637564352169508892'),
    'Hunter': ('637564202130866186','637564202021814277','637564202084466708'),
    'Shaman': ('936357056951222322','637564379595931649','637564379847458846'),
    'Mage': ('637564231545389056','637564231239073802','637564231469891594'),
    'Warlock': ('637564406682877964','637564407001513984','637564406984867861'),
    'Priest': ('637564323291725825','637564323442720768','637564323530539019'),
    'Evoker': ('1013161648728592444','1127164727328510054','1013161651706544158'),
}
ICON_CLASS = {icon: cls for cls, ids in SPEC_CLASSES.items() for icon in ids}


def readable_name(value):
    # Enclosed alphabet emoji are not present in the recap fonts; retain their letters.
    return ''.join(chr(ord('A')+ord(c)-0x1F170) if 0x1F170<=ord(c)<=0x1F189 else c for c in unicodedata.normalize('NFKC',value))


def members(event, profiles):
    result = {}
    choices = {(c['className'],c['specName']):c for c in raids.choices({**event,'classes':event.get('classes',[])})}
    for p in event['signUps']:
        group=p.get('roleName') or p.get('className') or 'Attending'
        group={'Tanks':'Tank','Healers':'Healer'}.get(group,group)
        name=profiles.get(str(p['userId']),{}).get('name')
        if not name or name=='Unknown member':name=p.get('name') or 'Former member'
        choice=choices.get((p.get('className'),p.get('specName')), {})
        source=str(p.get('specEmoteId') or choice.get('emoji_id') or ROLE_IDS.get(group,''))
        result.setdefault(group,[]).append({'name':readable_name(name),'class':ICON_CLASS.get(source,p.get('className','')),
            'spec':raids.spec_label(p.get('specName')),'icon':source})
    return result


def load_icons(groups):
    wanted=set(ROLE_IDS.values()) | {p['icon'] for rows in groups.values() for p in rows}
    def load(source):
        icon=EMOJIS.get(source)
        if not icon:return source,None
        try:
            response=httpx.get('https://cdn.discordapp.com/emojis/'+icon['id']+'.png?size=64',timeout=5)
            response.raise_for_status()
            if len(response.content)>200000:return source,None
            image=Image.open(io.BytesIO(response.content))
            if image.width*image.height>65536:return source,None
            return source,image.convert('RGBA')
        except (httpx.HTTPError,OSError,ValueError):return source,None
    with ThreadPoolExecutor(max_workers=4) as pool:return dict(pool.map(load,wanted))


def render(event, profiles, icons=None):
    groups=members(event,profiles)
    if icons is None:icons=load_icons(groups)
    blocks=[['Tank','Healer'],['Melee','Ranged']]
    others=[k for k in groups if k not in ROLE_IDS and k not in ('Absence','Tentative','Bench')]
    if others:blocks.append(others)
    blocks.append(['Absence','Tentative','Bench'])
    blocks=[[k for k in block if k in groups] for block in blocks]
    blocks=[block for block in blocks if block]
    height=160+sum(50+max(len(groups[k]) for k in block)*48+18 for block in blocks)+30
    image=Image.new('RGB',(1280,height*2),style.BG)
    canvas=style._Canvas(image,ImageDraw.Draw(image),{})
    def text(x,y,value,size=16,color=style.INK,weight='regular'):
        canvas.text(x,y,str(value),canvas.font(weight,size),color)
    def wrapped(value,size,width):
        lines=[];line=''
        for word in str(value).split():
            next_line=(line+' '+word).strip()
            if line and canvas.width(next_line,canvas.font('semibold',size))>width:
                lines.append(line);line=word
            else:line=next_line
        return lines+[line]
    def icon(source,x,y,size=20):
        asset=icons.get(source)
        if asset:
            tile=asset.resize((size*2,size*2),Image.Resampling.LANCZOS)
            image.paste(tile,(int(x*2),int(y*2)),tile)
    canvas.rect(0,0,640,40,fill=style.TOPBAR_BG)
    text(20,8,'ryangrey.dev',19,weight='semibold');text(539,9,'greyBot',18,style.MUTED)
    text(20,54,'RAID SIGNUPS',12,style.MUTED)
    title_lines=wrapped(event['title'],22,600)
    for i,line in enumerate(title_lines):text(20,74+i*28,line,22,weight='bold')
    extra=max(0,len(title_lines)-1)*28
    if extra:
        expanded=Image.new('RGB',(1280,(height+extra)*2),style.BG);expanded.paste(image,(0,0));image=expanded
        canvas.image=image;canvas.draw=ImageDraw.Draw(image)
    confirmed=sum(len(v) for k,v in groups.items() if k not in ('Late','Tentative','Absence','Bench'))
    tentative=sum(len(groups.get(k,[])) for k in ('Late','Tentative'))
    text(20,108+extra,f'{confirmed} (+{tentative})',20,style.ACCENT,'bold')
    for x,role,count in [(260,'Tank',len(groups.get('Tank',[]))),(370,'Healer',len(groups.get('Healer',[]))),
                         (480,'Melee',len(groups.get('Melee',[]))+len(groups.get('Ranged',[])))]:
        icon(ROLE_IDS[role],x,110+extra);text(x+28,108+extra,count,20,weight='semibold')
    y=154+extra
    for block in blocks:
        gap=16;width=(600-gap*(len(block)-1))/len(block)
        panel_height=50+max(len(groups[k]) for k in block)*48
        for column,group in enumerate(block):
            x=20+column*(width+gap)
            canvas.rect(x,y,x+width,y+panel_height,fill=style.BG,outline=style.LINE,radius=6)
            canvas.rect(x+1,y+1,x+width-1,y+36,fill=style.CHIP,radius=5)
            label={'Tank':'Tanks','Healer':'Healers'}.get(group,group)
            if group in ROLE_IDS:icon(ROLE_IDS[group],x+10,y+9,18)
            elif group == 'Bench':
                for left,top,right,bottom in [(10,8,13,25),(10,22,26,25),(23,24,26,31),(10,24,13,31)]:
                    canvas.rect(x+left,y+top,x+right,y+bottom,fill=style.ACCENT)
            elif group in ('Absence','Tentative'):
                text(x+10,y+6,{'Absence':'×','Tentative':'?'}[group],19,style.ACCENT,'bold')
            text(x+34,y+9,f'{label} · {len(groups[group])}',14,weight='semibold')
            for number,p in enumerate(groups[group],1):
                ry=y+44+(number-1)*48
                text(x+9,ry,f'{number}.',12,style.MUTED)
                icon(p['icon'],x+30,ry+1,17)
                color=recap_page.class_color(p['class']) or '#f0f6fc'
                name_size=16
                while name_size>10 and canvas.width(p['name'],canvas.font('semibold',name_size))>width-60:name_size-=1
                text(x+54,ry,p['name'],name_size,color,'semibold')
                if p['spec']:text(x+54,ry+21,p['spec'],12,color)
        y+=panel_height+18
    text(20,y,'greyBot · Use the buttons below to update your signup',11,style.MUTED)
    output=io.BytesIO();image.save(output,format='PNG');return output.getvalue()
