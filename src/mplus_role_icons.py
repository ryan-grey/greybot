"""Use the same role artwork as the server's raid signup controls."""
from functools import lru_cache
from pathlib import Path

EMOJI = {
    'tank': '<:raid_878310168289505301:1546964094869831763>',
    'healer': '<:raid_898011741735235645:1546964096899883040>',
    'dps': '<:raid_734439523328720913:1546964095805300837>',
}

@lru_cache(maxsize=3)
def raw(role):
    if role not in EMOJI:
        return None
    root = Path(__file__).resolve().parent
    folder = root / 'role-icons'
    if not folder.is_dir():
        folder = root.parent / 'assets' / 'role-icons'
    return (folder / (role + '.png')).read_bytes()

def html(role):
    data = raw(role)
    if not data:
        return '<span class="role role-none" aria-hidden="true"></span>'
    label = {'tank':'Tank','healer':'Healer','dps':'Damage'}[role]
    emoji_id = EMOJI[role].rsplit(':', 1)[1].rstrip('>')
    return f'<img class="role" width="14" height="14" alt="{label}" aria-label="{label}" src="https://cdn.discordapp.com/emojis/{emoji_id}.png">'
