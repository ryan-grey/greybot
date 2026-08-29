"""Warcraft Logs v2 client — the EVENT source: which boss died, and when.

Two things about this API shape the code more than anything else.

First, the rate limit is points per hour, not requests per hour, and a query's cost
depends on what you ask for rather than how often you ask. Guessing a "safe" poll
interval is therefore guessing at the wrong variable. Every query here asks for
`rateLimitData` alongside the real payload -- it rides along for free -- and the caller
backs off on the number it reports rather than on a hardcoded interval.

Second, a fight's startTime is a millisecond OFFSET from its report's startTime, not an
absolute timestamp. Treating it as absolute puts every kill in January 1970, which is
only obvious once an AOTC announcement is dated 56 years ago.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

# WCL difficulty ids for retail raids. Heroic is 4; the others are here so the constant
# is self-documenting rather than a bare 4 three files away from its meaning.
LFR, NORMAL, HEROIC, MYTHIC = 1, 3, 4, 5

_token = {"value": None, "expires_at": 0.0}


class WCLError(RuntimeError):
    pass


def _post(url, data, headers, timeout=20):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise WCLError(f"HTTP {exc.code} from {urllib.parse.urlparse(url).path}: {body}") from exc
    except urllib.error.URLError as exc:
        raise WCLError(f"network error calling {urllib.parse.urlparse(url).path}: {exc.reason}") from exc


def get_token(client_id, client_secret, now=None):
    """Client-credentials token, cached in module scope across warm invocations.

    WCL issues these with a very long life, so re-minting one on every poll is pure waste.
    The 60s safety margin is there because the token is checked here but spent a moment
    later; expiring in between would surface as a confusing 401 on the real query.
    """
    now = now if now is not None else time.time()
    if _token["value"] and now < _token["expires_at"] - 60:
        return _token["value"]

    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    import base64
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload = _post(TOKEN_URL, body, {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    tok = payload.get("access_token")
    if not tok:
        raise WCLError("token response carried no access_token")
    _token["value"] = tok
    _token["expires_at"] = now + float(payload.get("expires_in", 3600))
    return tok


def query(token, document, variables=None):
    body = json.dumps({"query": document, "variables": variables or {}}).encode()
    payload = _post(API_URL, body, {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    if payload.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in payload["errors"])
        raise WCLError(f"GraphQL errors: {msgs}")
    return payload.get("data") or {}


RATE = "rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }"

RATE_ONLY_Q = """
query { %s }
""" % RATE

GUILD_Q = """
query($name: String!, $server: String!, $region: String!) {
  %s
  guildData {
    guild(name: $name, serverSlug: $server, serverRegion: $region) {
      id
      name
      server { slug region { slug } }
    }
  }
}
""" % RATE

REPORTS_Q = """
query($guildID: Int!, $start: Float!, $limit: Int!, $difficulty: Int!) {
  %s
  reportData {
    reports(guildID: $guildID, startTime: $start, limit: $limit) {
      data {
        code
        startTime
        endTime
        zone { id name }
        fights(killType: Kills, difficulty: $difficulty) {
          id
          encounterID
          name
          kill
          difficulty
          startTime
          endTime
        }
      }
    }
  }
}
""" % RATE


def rate_limit(data):
    """Pull rateLimitData out of any response that carried it. Never raises -- a missing
    block means 'unknown', and unknown must not take down an announcement."""
    r = (data or {}).get("rateLimitData") or {}
    try:
        limit = int(r.get("limitPerHour") or 0)
        spent = float(r.get("pointsSpentThisHour") or 0.0)
        resets = int(r.get("pointsResetIn") or 0)
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    return {"limit": limit, "spent": spent, "resetsIn": resets,
            "fraction": round(spent / limit, 4)}


def find_guild(token, name, realm_slug, region):
    data = query(token, GUILD_Q, {"name": name, "server": realm_slug,
                                  "region": region.upper()})
    guild = ((data.get("guildData") or {}).get("guild")) or None
    return guild, rate_limit(data)


def heroic_kills_since(token, guild_id, since_ms, limit=12, difficulty=HEROIC):
    """Every Heroic boss KILL in the guild's reports since `since_ms`, newest report first.

    Returns a flat, chronologically ascending list of kills so the caller can announce a
    raid night in the order it actually happened rather than in whatever order the reports
    came back.
    """
    data = query(token, REPORTS_Q, {"guildID": int(guild_id), "start": float(since_ms),
                                    "limit": int(limit), "difficulty": int(difficulty)})
    reports = (((data.get("reportData") or {}).get("reports") or {}).get("data")) or []
    kills = []
    for rep in reports:
        base = rep.get("startTime") or 0
        zone = rep.get("zone") or {}
        for f in rep.get("fights") or []:
            if not f.get("kill"):
                continue
            if int(f.get("difficulty") or 0) != int(difficulty):
                continue
            enc = f.get("encounterID")
            if not enc:                      # trash and non-encounter kills carry 0
                continue
            kills.append({
                "encounterID": int(enc),
                "name": f.get("name") or f"Encounter {enc}",
                "zoneID": zone.get("id"),
                "zoneName": zone.get("name") or "",
                "reportCode": rep.get("code"),
                # fight times are offsets from the report start; see the module docstring
                "killedAtMs": int(base + (f.get("endTime") or f.get("startTime") or 0)),
            })
    kills.sort(key=lambda k: k["killedAtMs"])
    return kills, rate_limit(data)
