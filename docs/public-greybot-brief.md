# Project #9 — Public greyBot on CDK

**Status:** brief only — nothing built. Written Sept 1, 2026 (Cowork, at Ryan's direction).
**Decision:** Watchtower Pro is archived. Project #9 is generalizing greyBot from a
single-guild personal bot into an installable, multi-tenant Discord app, with its
infrastructure rebuilt in **AWS CDK**. Two portfolio gaps close at once: first product
with real external users, and the infrastructure-as-code framework line that AWS job
listings ask for.

## What exists (do not regress)

The current bot: Lambda + EventBridge poller, DynamoDB state, API Gateway HTTP
interactions with Ed25519 verification, SSM/KMS config, exactly-once posting via
conditional writes, the two-team classifier that posts nothing rather than guess,
`/progress` from a single GetItem, log-source-dark detection, email-on-eject,
341-check offline suite gating deploys, ~$0.02/month. All of that behavior is the
baseline; the public version must not lose the exactly-once and fail-quiet guarantees
that make it trustworthy in a live channel.

## Hard problems worth solving (these are the CV bullets)

1. **Multi-tenancy in the data model.** Everything keyed per Discord guild id:
   config, raid-team mapping, progression snapshots, dedupe state. Single table,
   Query-only on the partition key — same discipline as GreyScale. No tenant can
   read or affect another's rows, enforced in the key derivation (guild id comes
   from the verified interaction payload, never from a user-supplied field —
   the GreyScale "no endpoint accepts a member id" rule, transplanted).
2. **The WCL request budget is the scaling constraint.** The API budget
   (~3,600 pts/hr) is shared across ALL tenants — a naive per-guild poll
   multiplies cost linearly and dies at maybe a dozen installs. Design needed:
   shared-upstream caching (boss/zone metadata is identical for every tenant on a
   tier; many guilds resolve to the same public reports), per-tenant polling tiers
   (active raid night vs. idle week), token-bucket budgeting with per-tenant
   fairness, and graceful degradation that visibly says "data delayed" rather than
   silently staling. This is the distinctive engineering story of the project.
   **REVISED — decide the ceiling now, in Phase 1, and publish it.** A shared
   ~3,600 pts/hr budget means a maximum install count exists whether or not anyone
   names it. Naming it up front turns it from a production incident at install ~40
   into a designed limit: pick a target (say N tenants at a stated poll cadence),
   derive the per-tenant point allowance from it, and make Phase 3's load test prove
   that number rather than discover it. Two things follow that are much cheaper to
   build in than to retrofit — a **hard admission check** that refuses or waitlists
   an install past the ceiling instead of silently degrading everyone already on,
   and **cost-and-points per tenant as a measured metric** from the first
   multi-tenant deploy, since the brief already promises a quotable
   cost-per-tenant number and that is only quotable if it was instrumented from
   the start. Note this also bounds project #10's timeline: the App Directory needs
   75+ installs, so the ceiling must be comfortably above 75 or listing is
   unreachable by construction.
3. **Self-serve onboarding.** `/setup` wizard over HTTP interactions: pick
   guild/realm/region, validate against the APIs before saving, choose the posting
   channel, preview a card. Config lives per-tenant in DynamoDB (SSM stays for
   bot-level secrets only — paths, never values, in any file or doc).
4. **Tenant lifecycle.** Install (OAuth2 add-to-server), channel permission loss,
   eject (already detected — now must clean up or suspend that tenant's polling
   instead of emailing Ryan), re-install. Poller fan-out must skip suspended
   tenants for free.
5. **CDK end-to-end.** The whole stack — functions, tables, schedules, API, alarms —
   as a CDK app with dev and prod stages. Phase 1 is porting the CURRENT
   single-tenant bot to CDK with deploy parity and zero behavior change;
   multi-tenancy lands on top of that. **REVISED:** this used to read "the existing
   suite is the parity check" — it is not, and cannot be. See Phase 1 below.
6. **Per-tenant observability.** A tenant's failure (bad config, revoked channel)
   must not page Ryan or affect others; aggregate health still alarms through the
   existing CloudWatch → email path.

## Phases

1. CDK-ify the existing bot, deploy parity, suite green. (IaC bullet earned here.)
   **REVISED — the 341-check suite cannot be the parity gate.** It is an *offline*
   suite: no AWS, no network, no credentials. That is exactly what makes it a good
   deploy gate, and exactly why it cannot detect infrastructure drift. It would stay
   green while CDK deployed a different IAM policy, a different schedule expression,
   a different memory size, a missing DLQ, or an env var pointing at the wrong SSM
   path — every one of which is a behaviour change in production and invisible to a
   test that never leaves the machine.
   Parity has to be checked against real resources. Cheapest honest version:
   `describe-*` the live single-tenant stack into a snapshot BEFORE any CDK work
   starts, then diff `cdk synth` output against that snapshot and require an
   explicit, reviewed allowlist for every difference. Add a post-deploy smoke test
   that exercises the interactions endpoint and one poll cycle against the CDK stack.
   The 341-check suite still gates deploys as it does today — it just is not evidence
   of parity, and treating it as such would let Phase 1 "pass" while silently
   regressing the guarantees the whole project rests on.
2. Tenant data model + `/setup` + per-guild posting; run scrambled as tenant #1
   with zero special-casing.
3. Upstream budgeting/caching layer; load-test the fan-out with synthetic tenants.
4. Public listing: Discord App Directory requirements, terms/privacy pages
   (greybot-terms.html / greybot-privacy.html already exist on ryangrey.dev),
   support server (Ryan's planned GREY-tag server is its home).
5. Per-tenant health surfaces + quotas.

## Constraints

- Costs stay serverless/on-demand; target well under $1/month until install count
  justifies more. Cost-per-tenant should be a measured, quotable number.
- No credentials in writing anywhere — SSM parameter paths only (/greybot/...).
- The 341-check suite grows with every phase and keeps gating deploys.
- Ryan's lane split stands: Claude Code owns this repo and build; Cowork handles
  CV updates when phases ship.
