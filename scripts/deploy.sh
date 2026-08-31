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
# Credited on the AOTC card only. Left empty until the repository is actually public --
# a credit that 404s is worse than no credit.
REPO_URL="${REPO_URL:-}"

PARAMS=(/greybot/wcl/client_id /greybot/wcl/client_secret /greybot/discord/webhook_url
        /greybot/discord/prog_role_id /greybot/guild/name /greybot/guild/realm
        /greybot/guild/region /greybot/blizzard/client_id
        /greybot/blizzard/client_secret /greybot/discord/bot_token
        /greybot/discord/public_key /greybot/discord/guild_id
        /greybot/recap/enabled /greybot/recap/show_worst_parse /greybot/recap/schedule
        /greybot/team/roster_min_first_kill_pct /greybot/team/prog_overlap_high
        /greybot/team/prog_overlap_low /greybot/team/prog_tag
        /greybot/alerts/sns_topic_arn)

# The self-test needs PyNaCl to exercise signature verification against real Ed25519
# rather than a stub, and a stub would happily agree with an implementation that had the
# arguments the wrong way round. Kept in a venv so nothing is installed system-wide.
VENV="$ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> Creating .venv for test dependencies"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
fi
"$VENV/bin/python" -c 'import nacl' 2>/dev/null || "$VENV/bin/pip" install --quiet pynacl

echo "==> Self-test (blocks the deploy on failure)"
"$VENV/bin/python" "$ROOT/scripts/selftest.py"

echo "==> Packaging"
TMP="$(mktemp -d)"
cp "$ROOT/src/"*.py "$TMP/"

# PyNaCl is the one dependency, and it is native, so the wheel has to match the Lambda's
# architecture rather than this laptop's. Downloading the aarch64 wheel directly avoids
# needing Docker to cross-build. Signature verification is not somewhere to hand-roll
# crypto: libsodium is the audited implementation and this is the audited binding to it.
echo "    vendoring PyNaCl (linux/aarch64)"
python3 -m pip install --quiet --platform manylinux2014_aarch64 --implementation cp \
  --python-version 3.12 --only-binary=:all: --target "$TMP" pynacl
rm -rf "$TMP"/bin "$TMP"/*.dist-info

( cd "$TMP" && zip -qr package.zip . )
echo "    package: $(du -h "$TMP/package.zip" | cut -f1)  (stdlib + boto3 + PyNaCl)"

ENV="$(python3 - "$TABLE" "$ANNOUNCE_TZ" "$REPO_URL" <<'PY'
import json, sys
table, tz, repo = sys.argv[1:4]
print(json.dumps({"Variables": {"STATE_TABLE": table, "ANNOUNCE_TZ": tz,
                                "REPO_URL": repo}}))
PY
)"

if aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  echo "[=] Updating $FN"
  # Architecture rides on update-function-code, which is the only update call that accepts
  # it -- update-function-configuration rejects --architectures outright. That is the right
  # shape anyway: the aarch64 binaries and the arm64 metadata land in the same call, so
  # there is never a moment where one points at the other.
  CUR_ARCH="$(aws lambda get-function-configuration --function-name "$FN" \
    --region "$REGION" --query 'Architectures[0]' --output text)"
  [ "$CUR_ARCH" = "arm64" ] || echo "    switching architecture $CUR_ARCH -> arm64"
  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    --zip-file "fileb://$TMP/package.zip" --architectures arm64 >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN" --region "$REGION" \
    --environment "$ENV" --timeout 60 --memory-size 512 >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  FIRST_DEPLOY=0
else
  echo "[+] Creating $FN"
  aws lambda create-function --function-name "$FN" --region "$REGION" \
    --runtime python3.12 --handler handler.handler --role "$ROLE" \
    --architectures arm64 \
    --zip-file "fileb://$TMP/package.zip" \
    --environment "$ENV" --timeout 60 --memory-size 512 >/dev/null
  aws lambda wait function-active-v2 --function-name "$FN" --region "$REGION"
  FIRST_DEPLOY=1
fi
rm -rf "$TMP"

# Deliberately NOT reserving concurrency of 1 any more. It was harmless when this function
# only polled, but the same function now serves Discord interactions on a three-second
# deadline, and a slash command queued behind a running poll would fail visibly in chat.
# The conditional writes are what make overlapping polls safe, not the concurrency cap.

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
PENDING=0
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
else
  # Absent is not the same as broken. The schedule is deliberately the LAST step of setup,
  # so on any deploy that happens before step 5 it is simply not there yet. Failing the
  # deploy over it would be the same error as treating AccessDenied as "missing": the
  # deploy itself succeeded, and the code is live either way. Loud, but not fatal.
  echo "    [--] schedule $SCHEDULE not created yet — run infra/create-schedule.sh (step 5)"
  PENDING=1
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
if [ "$PENDING" = "1" ]; then
  echo
  echo "The function is live but nothing is invoking it yet. That is expected until"
  echo "infra/create-schedule.sh runs in CloudShell as the last setup step."
fi
[ "$DRIFT" = "0" ] || { echo; echo "Drift detected — fix in CloudShell as admin (infra/iam-setup.sh)."; exit 1; }
