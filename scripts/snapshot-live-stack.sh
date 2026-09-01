#!/usr/bin/env bash
#
# Snapshot every live AWS resource the hand-rolled deploy created, BEFORE any CDK
# work touches them. This file is the parity baseline for Phase 1.
#
# ---------------------------------------------------------------------------
# Why this exists at all
#
# The brief originally named the 341-check suite as Phase 1's parity gate. It
# cannot be. That suite is offline by design -- no AWS, no network, no
# credentials -- which is exactly what makes it a good deploy gate and exactly
# why it cannot see infrastructure drift. It stays green through a changed IAM
# policy, a different schedule expression, a smaller memory size, a missing DLQ,
# or an env var pointing at the wrong SSM path. Every one of those is a
# production behaviour change and none of them are visible to a test that never
# leaves the machine.
#
# So parity is checked against real resources: capture what is deployed now,
# then diff `cdk synth` against this. Anything that differs must be explained and
# allowlisted deliberately, not discovered after the fact.
#
# RUN THIS BEFORE THE FIRST CDK COMMIT. Once CDK has touched the account, the
# baseline is no longer a record of the hand-rolled stack, and there is no way to
# recover it.
# ---------------------------------------------------------------------------
#
# Secrets: SSM parameter VALUES are never read. Only names, types and versions.
# The repo rule is paths, never values, in any file or doc.
#
# Usage:  AWS_PROFILE=infra ./scripts/snapshot-live-stack.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${REGION:-us-east-1}"
FN="${FUNCTION_NAME:-ryangrey-greybot}"
ROLE_NAME="${ROLE_NAME:-ryangrey-greybot-role}"
TABLE="${STATE_TABLE:-ryangrey-greybot}"
SCHEDULE="${SCHEDULE_NAME:-ryangrey-greybot-poll}"

OUT="$ROOT/docs/parity-baseline"
mkdir -p "$OUT"

say() { printf '==> %s\n' "$1"; }
grab() { # grab <file> <aws args...>
    local f="$1"; shift
    if "$@" --region "$REGION" > "$OUT/$f" 2>"$OUT/$f.err"; then
        rm -f "$OUT/$f.err"; echo "    $f"
    else
        echo "    $f -- FAILED, see $f.err" >&2
    fi
}

say "Lambda $FN"
grab lambda-configuration.json aws lambda get-function-configuration --function-name "$FN"
grab lambda-policy.json        aws lambda get-policy               --function-name "$FN"
grab lambda-concurrency.json   aws lambda get-function-concurrency --function-name "$FN"

say "DynamoDB $TABLE"
grab dynamodb-table.json aws dynamodb describe-table --table-name "$TABLE"
grab dynamodb-ttl.json   aws dynamodb describe-time-to-live --table-name "$TABLE"
# Item count is a live fact, not config, but it is the number that proves the
# import kept the dedupe rows. Record it now so the after can be compared.
grab dynamodb-itemcount.json aws dynamodb scan --table-name "$TABLE" --select COUNT

say "EventBridge Scheduler $SCHEDULE"
grab scheduler.json aws scheduler get-schedule --name "$SCHEDULE"

say "IAM $ROLE_NAME"
grab iam-role.json aws iam get-role --role-name "$ROLE_NAME"
if POLICIES=$(aws iam list-role-policies --role-name "$ROLE_NAME" --region "$REGION" \
              --query 'PolicyNames[]' --output text 2>/dev/null); then
    echo "$POLICIES" > "$OUT/iam-inline-policy-names.txt"
    for p in $POLICIES; do
        grab "iam-inline-$p.json" aws iam get-role-policy --role-name "$ROLE_NAME" --policy-name "$p"
    done
fi
grab iam-attached.json aws iam list-attached-role-policies --role-name "$ROLE_NAME"

say "API Gateway (interactions endpoint)"
if API_ID=$(aws apigatewayv2 get-apis --region "$REGION" \
            --query "Items[?contains(Name,'greybot')].ApiId" --output text 2>/dev/null) \
   && [ -n "$API_ID" ] && [ "$API_ID" != "None" ]; then
    echo "$API_ID" > "$OUT/api-id.txt"
    grab api.json        aws apigatewayv2 get-api    --api-id "$API_ID"
    grab api-routes.json aws apigatewayv2 get-routes --api-id "$API_ID"
    grab api-stages.json aws apigatewayv2 get-stages --api-id "$API_ID"
    grab api-integrations.json aws apigatewayv2 get-integrations --api-id "$API_ID"
else
    echo "    no greybot HTTP API found" >&2
fi

say "SSM parameters (names and metadata only -- never values)"
aws ssm describe-parameters --region "$REGION" \
    --parameter-filters "Key=Name,Option=BeginsWith,Values=/greybot/" \
    --query 'Parameters[].{Name:Name,Type:Type,Version:Version,LastModified:LastModifiedDate}' \
    > "$OUT/ssm-parameters.json" 2>/dev/null && echo "    ssm-parameters.json"

say "CloudWatch alarms"
grab alarms.json aws cloudwatch describe-alarms --alarm-name-prefix greybot

say "Log group"
grab log-group.json aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/$FN"

# A single sorted digest makes "did anything move" one diff rather than twenty.
say "Digest"
( cd "$OUT" && for f in *.json; do
      printf '%s  %s\n' "$(shasum -a 256 "$f" | cut -d' ' -f1)" "$f"
  done | sort -k2 ) > "$OUT/SHA256SUMS"
echo "    SHA256SUMS ($(grep -c . "$OUT/SHA256SUMS") files)"

cat > "$OUT/README.md" <<'MD'
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
MD
echo "    README.md"

echo
echo "Baseline in $OUT"
echo "Commit it before the first CDK commit."
