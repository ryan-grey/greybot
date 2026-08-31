# Handoff: Warcraft Logs `reportData.reports()` returns empty for every guild

**Status: RECOVERED on its own at ~19:30 UTC on 2026-08-31, cause still unknown.**
The API started answering again between 19:00 and 19:31 UTC with no action taken and no
code change. This is now an **incident record and a retrospective research brief**, not a
live problem — do not go looking for a broken thing, it is not broken any more.

**Impact while it lasted:** ~18 hours, 01:03 → ~19:30 UTC. greyBot could not see any raid
logs and therefore could not detect a kill. Nothing was lost: the dedupe state stayed
intact and no announcement was skipped, because no kill was ever *detected* rather than
detected-and-dropped. Scrambled did not raid in the window, so nothing was actually missed.

**Why it is still worth researching.** An 18-hour silent outage in the one API this bot
depends on will happen again, and it would be useful to know whether it was announced
anywhere, whether other consumers saw it, and whether there is a status feed worth
watching. It also went undetected for 18 hours, which is the part that has now been fixed
(see "What was built as a result").

**Please do not trust the conclusions in this file without re-running the checks — one
confident conclusion in this investigation was already wrong** (see "A wrong turn worth
knowing about"), and a second was wrong in the other direction: the recovery was found
only because a newly added probe reported `reportsVisible: 1` while the investigation was
still describing the API as down.

---

## The one-sentence version

`reportData.reports(guildID: …)` on the Warcraft Logs v2 client API returns a **200 with an
empty `data` array for every guild we try**, including guilds whose logs are unambiguously
public, while every other part of the same API answers normally with the same token.

---

## Timeline (UTC)

| when | what |
|---|---|
| through 2026-08-31 01:03 | normal. 191 × `poll_done`, kills seen and correctly deduped |
| **01:03:21** | last successful poll — `{"event":"poll_done","kills":3,...}` |
| **01:18:20** | first blind poll. Zero reports |
| 01:18 → ~19:00 | ~72 consecutive blind polls, every 15 minutes |
| **19:00** | still blind. Scrambled, Liquid **and Echo** all return 0 reports |
| **19:31** | recovered. All three return reports again; global query returns reports |

No deploy happened in that gap. The greyBot code changes that day landed hours later
(17:30+), and reverting them would not touch this — the blindness predates them.

The transition is sharp: one poll worked, the next did not, 15 minutes apart.

---

## What still works

Same OAuth2 client-credentials token, same endpoint, same session:

```
guildData.guild(name/serverSlug/serverRegion)   works — resolves Scrambled to id 655330
guildData.guild(...) for Liquid, Echo           works — ids 488971, 1546
rateLimitData.limitPerHour / pointsSpent        works — 3600 limit, ~30 spent, 0.8%
OAuth2 /oauth/token client_credentials grant    works — token issues normally
```

So: auth is fine, the client is not rate-limited, the API is reachable, and at least one
other top-level subtree (`guildData`) returns real data.

## What does not work

```
reportData.reports(guildID: 655330, limit: 5)                    → 0
reportData.reports(guildID: 655330, startTime: 0, limit: 5)      → 0
reportData.reports(guildID: 655330, startTime: <30d ago>)        → 0
reportData.reports(limit: 3)            (no guild filter at all) → 0
```

**No GraphQL error. No HTTP error. A 200 with `data: []`.** That is the whole difficulty:
there is nothing to read an explanation off.

---

## What has been ruled out, and how

| hypothesis | ruled out by |
|---|---|
| Scrambled's logs went private | Liquid (488971) and Echo (1546) return 0 reports too. Both are among the most-logged guilds in the game and both are public |
| The 3-day poll window aged out the last raid night | Same 0 with `startTime: 0` and with no time filter at all |
| Wrong guild id | 655330 resolves via `find_guild` and matches the public URL `warcraftlogs.com/guild/reports-list/655330` |
| Rate limiting / point exhaustion | `rateLimitData` reports 30 of 3600 points spent, 0.8%. The poller's own `POINTS_CEILING` backoff never fired |
| Bad or expired credentials | The token issues, and `guildData` queries with it return real data |
| Our code / a bad deploy | The transition is 01:03 → 01:18 with no deploy. Reproduced independently of the Lambda, from a laptop, calling `src/wcl.py` directly |
| Query complexity rejection | Those are returned as an explicit error before any data. We get a 200 with an empty array |

## A wrong turn worth knowing about

The first conclusion reached was **"the guild's logs went private."** It was wrong.

It came from greyBot's own log line, which says as much:

```json
{"event":"no_reports_visible","hint":"Raider.IO shows Heroic kills but Warcraft Logs
 returned no reports — the guild's logs are probably private to this OAuth client."}
```

That hint is a guess baked into the code, and it is the *only* explanation the code offers,
so it framed the whole investigation. What settled it was one control query: ask for a
different guild's reports. Liquid and Echo came back empty too, which no privacy setting on
Scrambled could explain.

**Run the control first.** The hint is being reworded as part of this work, but anything
reading older logs will still see it.

---

## How to reproduce

Credentials live in SSM (region `us-east-1`) and are readable with the `infra` profile:

```
/greybot/wcl/client_id        SecureString
/greybot/wcl/client_secret    SecureString
```

The repo's own client is the fastest way in — it handles the OAuth dance and the
rate-limit parsing:

```python
import sys; sys.path.insert(0, "src")
import wcl, subprocess

def ssm(n):
    return subprocess.run(["aws","--profile","infra","ssm","get-parameter","--name",n,
        "--with-decryption","--query","Parameter.Value","--output","text",
        "--region","us-east-1"], capture_output=True, text=True).stdout.strip()

tok = wcl.get_token(ssm("/greybot/wcl/client_id"), ssm("/greybot/wcl/client_secret"))

# control: a guild that is definitely public
g, _ = wcl.find_guild(tok, "Liquid", "illidan", "us")          # -> id 488971
d = wcl.query(tok, "query($g:Int!){reportData{reports(guildID:$g,limit:3){data{code}}}}",
              {"g": int(g["id"])})
print(d)   # observed: {"reportData": {"reports": {"data": []}}}
```

`wcl.query` raises `WCLError` on GraphQL errors, so a silent empty result really is empty
rather than a swallowed error. Worth re-verifying that assumption rather than taking it on
trust — see `src/wcl.py:74`.

Do **not** run this on a loop. Warcraft Logs bills points per hour, and the live bot shares
the same client and the same 3,600-point budget.

---

## The recovery, and what it rules out

At 19:31 UTC, with no action taken:

```
Scrambled  id=655330   reports: 3     (was 0 at 19:00)
Liquid     id=488971   reports: 3     (was 0 at 19:00)
Echo       id=1546     reports: 3     (was 0 at 19:00)
global, no guild filter: 3            (was 0)
```

Query shape was also tested at that point and is **not** implicated — `startTime` alone,
`startTime + endTime`, with `page`, and with the full `fights` subquery all returned the
same single report for the poll window. So the announcer's query was never wrong.

Self-recovery without intervention makes the second hypothesis below much less likely: a
deliberate permissions change to the client-credentials grant would not undo itself after
eighteen hours. **An incident is now the leading explanation.** That is not proof — an
incident is simply the hypothesis that best fits "broke sharply, stayed broken for 18
hours, recovered by itself, affected every guild equally."

## What was built as a result

`src/health.py` + `handler.run_source_check` now carry a `source_blind` probe. Every poll
where no kills are found asks whether any *reports* are visible — a cheap query with no
`fights` subquery, and only in that ambiguous case, so a normal poll pays nothing. Four
consecutive polls with zero reports while Raider.IO still shows Heroic progress raises
`SOURCE_BLIND` and emails, then reminds daily, then sends one all-clear. It would have
reported this incident at roughly 02:00 UTC instead of it being noticed 17 hours later.

The distinction that makes it usable: **blindness is measured on reports, not kills.** A
guild that has not raided in three days has no kills and is fine; zero reports while
Raider.IO shows progress is an outage.

## Open questions for research

1. **Is this affecting other API consumers?** The most valuable single answer. Check the
   Warcraft Logs Discord, their API changelog, GitHub issues on community wrappers
   (`warcraftlogs` npm, `python-warcraftlogs`, WCL-adjacent Discord bots), and any status
   page. A dated, corroborating report from another consumer settles cause and rough ETA at
   once.

2. **Did the client-credentials grant lose access to `reportData`?** The plausible
   candidate. WCL distinguishes the client endpoint (`/api/v2/client`, public data only)
   from the user endpoint (`/api/v2/user`, what an authorising user can see). If report
   listing moved behind the user endpoint, this is a permanent API change and not an
   incident — which would change what greyBot has to do next. Look for changelog or docs
   language about which fields each grant may read. Note `warcraftlogs.com/api/docs`
   returns **403** to non-browser clients, so it will need a real browser.

3. **Is `reports()` specifically affected, or all of `reportData`?** Not yet tested:
   `reportData.report(code: "…")` for a single known-public report code. If fetching a
   report *by code* works while *listing* does not, that is a much narrower fault and
   suggests a workaround — greyBot could conceivably discover codes another way. A code can
   be lifted from the public reports list page by hand (Cloudflare 403s scripted fetches).

4. **Does the guild `attendance` path still work?** A first attempt at
   `guildData.guild(id:).attendance` returned a null guild, but the query was probably
   malformed — `find_guild` resolves the same guild fine by name. Worth retrying properly.
   Attendance rows carry report codes and would be an alternative discovery route.

5. **Is there a `zoneID`/`gameZone` filter now required?** Grasping, but cheap to test: the
   API has evolved before, and a newly-mandatory filter defaulting to "nothing" would
   produce exactly this signature.

## What a good answer looks like

The service recovered on its own, so the useful output is no longer a fix. It is:

- **Corroboration.** Did anyone else report a Warcraft Logs API outage on 2026-08-31
  between roughly 01:00 and 19:30 UTC? A dated third-party account turns this from "we saw
  something odd" into a known event.
- **A watchable signal.** Does Warcraft Logs publish a status page, an incident feed, or an
  API changelog worth subscribing to? Cheaper than inferring outages from empty responses.
- **Recurrence risk.** Has this happened before? If `reportData` going quietly empty is a
  known failure mode, the four-poll threshold may want tuning, and the handoff is worth
  keeping as a runbook rather than a one-off.
- **The `/api/v2/user` question, answered calmly.** No longer urgent, but still worth
  knowing: is report listing on the client endpoint guaranteed, or is it something WCL
  could restrict later? That determines whether the user-OAuth flow is a contingency worth
  designing before it is needed.

Question 3 below (does `reportData.report(code:)` work while `reports()` does not?) is now
**untestable until it recurs** — record the answer if it ever happens again, since a
narrow fault would suggest a workaround rather than a wait.

---

## Constraints

- **Read-only.** Do not change SSM parameters, the Lambda, or any AWS resource.
- **Do not put WCL queries on a timer.** Shared point budget with the live bot.
- **No credentials in writing.** Reference the SSM paths, never the values.
- The live poller keeps running every 15 minutes throughout. It is harmless — it finds
  nothing and announces nothing — and its logs are a free running record of whether the
  problem has cleared: `aws --profile infra logs tail /aws/lambda/ryangrey-greybot --since 1h`

## Related

- `src/wcl.py` — the client. `REPORTS_Q` at line ~105 is the query that returns empty.
- `src/handler.py` — the poll path and the `no_reports_visible` branch.
- `src/health.py` — Discord-side health probes. They report **ok** throughout this, because
  they watch Discord standing and not the data source. The `source_blind` probe added
  alongside this file is what turns this class of failure into an email.
