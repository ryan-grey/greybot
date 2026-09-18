"""Use the same role artwork as the server's raid signup controls."""
import base64
from functools import lru_cache
from pathlib import Path

EMOJI = {
    'tank': '<:raid_878310168289505301:1546964094869831763>',
    'healer': '<:raid_898011741735235645:1546964096899883040>',
    'dps': '<:raid_734439523328720913:1546964095805300837>',
}

def folder():
    root = Path(__file__).resolve().parent
    packaged = root / 'role-icons'
    return packaged if packaged.is_dir() else root.parent / 'assets' / 'role-icons'

@lru_cache(maxsize=3)
def raw(role):
    if role not in EMOJI:
        return None
    return (folder() / (role + '.png')).read_bytes()

@lru_cache(maxsize=1)
def style():
    """The page's policy is img-src 'self' data:, so the artwork travels in the page, once per role."""
    rules = ['.role-art{display:inline-block;background:center/contain no-repeat}']
    for role in EMOJI:
        art = base64.b64encode((folder() / (role + '-28.png')).read_bytes()).decode()
        rules.append(f'.role-{role}{{background-image:url(data:image/png;base64,{art})}}')
    return '\n'.join(rules)

def html(role):
    if role not in EMOJI:
        return '<span class="role role-none" aria-hidden="true"></span>'
    label = {'tank':'Tank','healer':'Healer','dps':'Damage'}[role]
    return f'<span class="role role-art role-{role}" role="img" aria-label="{label}"></span>'
