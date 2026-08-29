#!/usr/bin/env bash
# Step 2 of 2 in CloudShell. Run this LAST -- after infra/iam-setup.sh, after
# scripts/deploy.sh, and after a hand-invocation has been confirmed to seed rather than
# announce.
#
# Two reasons for the ordering. EventBridge Scheduler validates its target when the
# schedule is created, so the function has to exist. And starting the schedule before the
# first run is verified means a fifteen-minute timer is already running while you are still
# checking whether run one posted a false AOTC into a live guild channel.
#
# Export ACCT before running:  export ACCT=123456789012
set -euo pipefail

ACCT="${ACCT:?export ACCT=<your 12-digit account id> first}"
REGION=us-east-1
FN=ryangrey-greybot
SCHED_ROLE=ryangrey-greybot-scheduler-role
SCHEDULE=ryangrey-greybot-poll

# Refuse to create a schedule pointing at a function that is not there, rather than letting
# Scheduler return a less obvious error.
aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1 || {
  echo "Lambda $FN does not exist yet. Run scripts/deploy.sh first." >&2
  exit 1
}

aws scheduler create-schedule --region $REGION --name $SCHEDULE \
  --schedule-expression 'rate(15 minutes)' \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target "{\"Arn\":\"arn:aws:lambda:$REGION:$ACCT:function:$FN\",
             \"RoleArn\":\"arn:aws:iam::$ACCT:role/$SCHED_ROLE\",
             \"RetryPolicy\":{\"MaximumRetryAttempts\":2}}"

echo
echo "Schedule $SCHEDULE created: every 15 minutes."
echo "Pause it any time with:"
echo "  aws scheduler update-schedule --name $SCHEDULE --region $REGION --state DISABLED \\"
echo "    --schedule-expression 'rate(15 minutes)' --flexible-time-window '{\"Mode\":\"OFF\"}' \\"
echo "    --target \"{\\\"Arn\\\":\\\"arn:aws:lambda:$REGION:$ACCT:function:$FN\\\",\\\"RoleArn\\\":\\\"arn:aws:iam::$ACCT:role/$SCHED_ROLE\\\"}\""
