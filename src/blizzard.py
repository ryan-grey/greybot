"""Blizzard Game Data API — per-boss art.

What this can and cannot get, since the distinction cost some time to establish: the
stylised Adventure Guide portraits are NOT in Blizzard's API. Blizzard say so directly on
their own forums, and Wowhead serves those from its own CDN. What IS available is the
creature MODEL render -- 600x600, the boss on a near-black field -- reached by resolving a
boss name to a journal encounter, then to a creature display id.

The render CDN itself needs no authentication:

    https://render.worldofwarcraft.com/us/npcs/zoom/creature-display-{id}.jpg

Credentials are only needed to discover which display id belongs to which boss. That
mapping never changes once found, so it is resolved once per boss and cached in DynamoDB;
steady-state announcing makes no Blizzard calls at all. Doing it through the API rather
than hand-mapping eight bosses is what makes next tier work on its own.
"""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth.battle.net/token"
API_HOST = "https://us.api.blizzard.com"
NAMESPACE = "static-us"
LOCALE = "en_US"
RENDER = "https://render.worldofwarcraft.com/us/npcs/zoom/creature-display-{}.jpg"

_token = {"value": None, "expires_at": 0.0}


class BlizzardError(RuntimeError):
    pass


def _request(url, data=None, headers=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise BlizzardError(f"HTTP {exc.code} from {urllib.parse.urlparse(url).path}: {body}") from exc
    except urllib.error.URLError as exc:
        raise BlizzardError(f"network error: {exc.reason}") from exc


def get_token(client_id, client_secret, now=None):
    """Client-credentials token, cached in module scope. Blizzard issues these with a
    24-hour life, so re-minting one per invocation would be pure waste."""
    now = now if now is not None else time.time()
    if _token["value"] and now < _token["expires_at"] - 60:
        return _token["value"]
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload = _request(TOKEN_URL,
                       data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                       headers={"Authorization": f"Basic {basic}",
                                "Content-Type": "application/x-www-form-urlencoded"})
    tok = payload.get("access_token")
    if not tok:
        raise BlizzardError("token response carried no access_token")
    _token["value"] = tok
    _token["expires_at"] = now + float(payload.get("expires_in", 86400))
    return tok


def _get(token, path, **params):
    params.setdefault("namespace", NAMESPACE)
    params.setdefault("locale", LOCALE)
    url = f"{API_HOST}{path}?{urllib.parse.urlencode(params)}"
    return _request(url, headers={"Authorization": f"Bearer {token}"})


def _name_of(value):
    """Blizzard returns names either as a plain string or as a locale map, depending on
    the endpoint and whether a locale was honoured. Handle both rather than guessing."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get(LOCALE) or next(iter(value.values()), "")
    return ""


def find_encounter(token, boss_name, normalize):
    """Journal encounter whose name matches this boss, or None.

    Search first because it is one call. It is a fuzzy endpoint, so the result is confirmed
    by normalised name equality rather than trusted -- searching "Sszorak" and accepting
    whatever ranks first is how a card ends up with the wrong boss's portrait on it.
    """
    want = normalize(boss_name)
    try:
        res = _get(token, "/data/wow/search/journal-encounter",
                   **{"name.en_US": boss_name, "orderby": "id", "_page": 1})
    except BlizzardError:
        return None
    for hit in (res.get("results") or []):
        data = hit.get("data") or {}
        if normalize(_name_of(data.get("name"))) == want:
            return int(data.get("id"))
    return None


def creature_display_id(token, encounter_id):
    """First creature display on an encounter, which is the boss itself.

    Multi-boss encounters list several creatures; the first is the one the encounter is
    named for, and a council fight has no single portrait to be right about anyway.
    """
    enc = _get(token, f"/data/wow/journal-encounter/{int(encounter_id)}")
    for creature in (enc.get("creatures") or []):
        display = (creature.get("creature_display") or {}).get("id")
        if display:
            return int(display)
    return None


def art_url(display_id):
    return RENDER.format(int(display_id)) if display_id else None


def resolve(token, boss_name, normalize):
    """boss name -> (creature display id, image url). (None, None) if not found."""
    enc = find_encounter(token, boss_name, normalize)
    if not enc:
        return None, None
    display = creature_display_id(token, enc)
    return display, art_url(display)
