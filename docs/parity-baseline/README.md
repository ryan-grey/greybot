# Parity baseline — the hand-rolled stack, before CDK

Captured by `scripts/snapshot-live-stack.sh`. This is what Phase 1 must reproduce.

**Do not regenerate this after CDK has deployed.** It is a record of the stack as
built by `scripts/deploy.sh`, and once CDK owns the resources there is no way to
recover it. If it needs correcting, correct it from git history.

## How to use it

1. `cdk synth` the Phase 1 stack.
2. Diff the synthesised resources against these files — IAM statements, the
   schedule expression, Lambda memory/timeout/runtime/env keys, the table's key
   schema, billing mode and TTL attribute, the API routes and integration.
3. Every difference is either fixed or written down here with a reason. An
   unexplained difference is a failed parity check, not a rounding error.

## Watch these specifically

They are the ones that change behaviour silently and that an offline test suite
cannot see:

- **IAM inline policy statements** — a widened or narrowed action changes what
  fails, and only in production.
- **Schedule expression and timezone** — the poller's cadence is load-bearing for
  the WCL points budget.
- **Lambda env var keys** — a renamed key reads as `None` at runtime, not as an
  error. The SSM *paths* live here; the values never do.
- **DynamoDB key schema, billing mode, TTL attribute** — the dedupe guarantee
  rests on the key schema and on conditional writes against it.
- **`dynamodb-itemcount.json`** — the count before the import. After `cdk import`,
  it must match. A short count means dedupe rows were lost, and a lost dedupe row
  is a re-announced boss kill in a live channel.
