#!/usr/bin/env bash
# Ship the Lambda, then verify the admin-owned wiring around it.
#
# The deploy identity has no IAM write, no SSM write and no EventBridge Scheduler write, so
# the role, the table, the parameters and the schedule are created once by an admin
# (infra/iam-setup.sh) and then left alone. Everything below that this identity cannot
# change is checked and reported rather than reconciled -- an update it can only ever be
# denied is noise, but silent drift on the schedule means the bot quietly stops announcing.
#
# Note what is NOT here: no guild name, realm, region, role id or webhook. All seven live
# in SSM and are read at runtime, so a deploy cannot disagree with what the bot is using.
#
# Idempotent: safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$ROOT/.env" ] && . "$ROOT/.env"
REGION="${REGION:-us-east-1}"
ACCOUNT="${ACCOUNT_ID:?set ACCOUNT_ID}"
FN="${FUNCTION_NAME:-ryangrey-greybot}"
ROLE="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME:-ryangrey-greybot-role}"
TABLE="${STATE_TABLE:-ryangrey-greybot}"
SCHEDULE="${SCHEDULE_NAME:-ryangrey-greybot-poll}"
ANNOUNCE_TZ="${ANNOUNCE_TZ:-America/New_York}"

PARAMS=(/greybot/wcl/client_id /greybot/wcl/client_secret /greybot/discord/webhook_url
        /greybot/discord/prog_role_id /greybot/guild/name /greybot/guild/realm
        /greybot/guild/region)

echo "==> Self-test (blocks the deploy on failure)"
python3 "$ROOT/scripts/selftest.py"

echo "==> Packaging"
TMP="$(mktemp -d)"
cp "$ROOT/src/"*.py "$TMP/"
( cd "$TMP" && zip -qr package.zip ./*.py )
echo "    package: $(du -h "$TMP/package.zip" | cut -f1)  (no dependencies — stdlib + boto3)"

ENV="$(python3 - "$TABLE" "$ANNOUNCE_TZ" <<'PY'
import json, sys
table, tz = sys.argv[1:3]
print(json.dumps({"Variables": {"STATE_TABLE": table, "ANNOUNCE_TZ": tz}}))
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
  FIRST_DEPLOY=0
else
  echo "[+] Creating $FN"
  aws lambda create-function --function-name "$FN" --region "$REGION" \
    --runtime python3.12 --handler handler.handler --role "$ROLE" \
    --zip-file "fileb://$TMP/package.zip" \
    --environment "$ENV" --timeout 60 --memory-size 256 >/dev/null
  aws lambda wait function-active-v2 --function-name "$FN" --region "$REGION"
  FIRST_DEPLOY=1
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

# AccessDenied is not the same answer as "missing", and treating them alike turns a
# correctly-scoped deploy identity into a failing deploy. ryan-cli deliberately has no
# ssm:GetParameter and no scheduler read -- it can ship code and nothing else -- so an
# authorisation failure here means "cannot verify from this identity", which is a note,
# not drift. Only a resource that genuinely does not exist is drift.
UNVERIFIED=0
for P in "${PARAMS[@]}"; do
  ERR="$(aws ssm get-parameter --name "$P" --region "$REGION" 2>&1 >/dev/null)" && {
    echo "    [ok] $P"; continue; }
  case "$ERR" in
    *AccessDenied*|*UnauthorizedOperation*)
      echo "    [--] $P — no permission to check from this identity (by design)"
      UNVERIFIED=1 ;;
    *ParameterNotFound*)
      echo "    [!!] $P DOES NOT EXIST — the bot cannot start without it"
      DRIFT=1 ;;
    *)
      echo "    [!!] $P — $(printf '%s' "$ERR" | tail -1)"
      DRIFT=1 ;;
  esac
done

SCHED_ERR="$(aws scheduler get-schedule --name "$SCHEDULE" --region "$REGION" \
  --query 'ScheduleExpression' --output text 2>&1 >/dev/null)" || true
SCHED="$(aws scheduler get-schedule --name "$SCHEDULE" --region "$REGION" \
  --query 'ScheduleExpression' --output text 2>/dev/null || true)"
if [ -n "$SCHED" ] && [ "$SCHED" != "None" ]; then
  echo "    [ok] schedule $SCHEDULE — $SCHED"
elif printf '%s' "$SCHED_ERR" | grep -qiE 'accessdenied|not authorized'; then
  echo "    [--] schedule $SCHEDULE — no permission to check from this identity (by design)"
  UNVERIFIED=1
elif [ "$FIRST_DEPLOY" = "1" ]; then
  echo "    [--] schedule $SCHEDULE not created yet — expected; it is the LAST step"
else
  echo "    [!!] schedule $SCHEDULE missing — the bot would never run"
  DRIFT=1
fi

echo
echo "Function: $FN  ($REGION)"
echo "Logs:     aws logs tail /aws/lambda/$FN --follow --region $REGION"
echo "Dry run:  aws lambda invoke --function-name $FN --region $REGION /dev/stdout"
echo "Identity: scripts/set-webhook-identity.py --check"

if [ "$FIRST_DEPLOY" = "1" ]; then
  cat <<'NOTE'

FIRST DEPLOY — the next invocation SEEDS and announces nothing.
Scrambled arrives with three cleared tiers and a fourth in progress, so run one must
record all of that silently. Invoke it once by hand and confirm the log says:

    {"event":"bootstrap_complete", ... "announced":0, "note":"SEEDED, did not announce..."}

If you see announced_kill or announced_aotc on the first run, stop the schedule.
NOTE
fi

if [ "$UNVERIFIED" = "1" ]; then
  echo
  echo "Some admin-owned resources could not be checked from this identity. That is the"
  echo "intended separation, not a problem — verify them from CloudShell if you want to be"
  echo "sure:  aws ssm get-parameters-by-path --path /greybot --recursive --query 'Parameters[].Name'"
fi
[ "$DRIFT" = "0" ] || { echo; echo "Drift detected — fix in CloudShell as admin (infra/iam-setup.sh)."; exit 1; }
