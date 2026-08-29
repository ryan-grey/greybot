#!/usr/bin/env bash
# Ship the Lambda, then verify the admin-owned wiring around it.
#
# Same split as the study engine: the deploy identity has no IAM write, no EventBridge
# Scheduler write and no SSM write, so the role, the table, the secrets and the schedule
# are created once from infra/iam-setup.sh by an admin and then left alone. Everything
# below that this identity cannot change is checked and reported rather than reconciled --
# an update it can only ever be denied is noise, but silent drift on the schedule means
# the bot quietly stops announcing.
#
# Idempotent: safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$ROOT/.env" ] && . "$ROOT/.env"
REGION="${REGION:-us-east-1}"
ACCOUNT="${ACCOUNT_ID:?set ACCOUNT_ID}"
FN=ryangrey-scrambled-bot
ROLE="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME:-ryangrey-scrambled-role}"
TABLE="${STATE_TABLE:-ryangrey-scrambled}"
SCHEDULE="${SCHEDULE_NAME:-ryangrey-scrambled-poll}"

GUILD_NAME="${GUILD_NAME:?set GUILD_NAME}"
GUILD_REALM="${GUILD_REALM:?set GUILD_REALM (lowercase-hyphenated realm slug)}"
GUILD_REGION="${GUILD_REGION:-us}"
PROG_RAIDER_ROLE_ID="${PROG_RAIDER_ROLE_ID:-}"
ANNOUNCE_TZ="${ANNOUNCE_TZ:-America/New_York}"
SEED_ONLY="${SEED_ONLY:-}"

echo "==> Self-test (blocks the deploy on failure)"
python3 "$ROOT/scripts/selftest.py"

echo "==> Packaging"
TMP="$(mktemp -d)"
cp "$ROOT/src/"*.py "$TMP/"
( cd "$TMP" && zip -qr package.zip ./*.py )
echo "    package: $(du -h "$TMP/package.zip" | cut -f1)  (no dependencies — stdlib + boto3)"

# JSON form rather than shorthand: PROG_RAIDER_ROLE_ID and SEED_ONLY are legitimately
# empty most of the time, and shorthand cannot express an empty value.
ENV="$(python3 - "$TABLE" "$GUILD_NAME" "$GUILD_REALM" "$GUILD_REGION" \
        "$PROG_RAIDER_ROLE_ID" "$ANNOUNCE_TZ" "$SEED_ONLY" <<'PY'
import json, sys
t, name, realm, region, role, tz, seed = sys.argv[1:8]
print(json.dumps({"Variables": {
    "STATE_TABLE": t, "GUILD_NAME": name, "GUILD_REALM": realm,
    "GUILD_REGION": region, "PROG_RAIDER_ROLE_ID": role,
    "ANNOUNCE_TZ": tz, "SEED_ONLY": seed}}))
PY
)"

if aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  echo "[=] Updating $FN"
  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    --zip-file "fileb://$TMP/package.zip" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN" --region "$REGION" \
    --environment "$ENV" --timeout 60 --memory-size 256 >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
else
  echo "[+] Creating $FN"
  aws lambda create-function --function-name "$FN" --region "$REGION" \
    --runtime python3.12 --handler handler.handler --role "$ROLE" \
    --zip-file "fileb://$TMP/package.zip" \
    --environment "$ENV" --timeout 60 --memory-size 256 >/dev/null
  aws lambda wait function-active-v2 --function-name "$FN" --region "$REGION"
fi
rm -rf "$TMP"

# One invocation at a time. The conditional writes already make overlap safe, but a poll
# that outruns its schedule has nothing useful to add and only spends Warcraft Logs points.
aws lambda put-function-concurrency --function-name "$FN" --region "$REGION" \
  --reserved-concurrent-executions 1 >/dev/null 2>&1 \
  && echo "    reserved concurrency: 1" \
  || echo "    [skip] no permission to set reserved concurrency (set it in the console)"

echo "==> Verifying admin-managed resources"
DRIFT=0

if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
  echo "    [ok] state table $TABLE"
else
  echo "    [!!] state table $TABLE MISSING — every poll would re-announce every kill"
  DRIFT=1
fi

for P in /scrambled/wcl/client_id /scrambled/wcl/client_secret /scrambled/discord/webhook_url; do
  if aws ssm get-parameter --name "$P" --region "$REGION" >/dev/null 2>&1; then
    echo "    [ok] $P"
  else
    echo "    [!!] $P missing or unreadable"
    DRIFT=1
  fi
done

SCHED="$(aws scheduler get-schedule --name "$SCHEDULE" --region "$REGION" \
  --query 'ScheduleExpression' --output text 2>/dev/null || true)"
if [ -n "$SCHED" ] && [ "$SCHED" != "None" ]; then
  echo "    [ok] schedule $SCHEDULE — $SCHED"
else
  echo "    [!!] schedule $SCHEDULE missing or unreadable — the bot would never run"
  DRIFT=1
fi

echo
echo "Function: $FN  ($REGION)"
echo "Logs:     aws logs tail /aws/lambda/$FN --follow --region $REGION"
echo "Dry run:  aws lambda invoke --function-name $FN --region $REGION /dev/stdout"
[ "$DRIFT" = "0" ] || { echo; echo "Drift detected — fix in CloudShell as admin (infra/iam-setup.sh)."; exit 1; }
