#!/usr/bin/env bash
# The Tuesday vault and gear check's schedule: 11:30 AM Eastern, after the US weekly reset
# (11 AM in summer, 10 AM in winter), invoking the same function with {"mode":"vault"}.
#
#     infra/create-vault-schedule.sh            # preview: prints what it would create
#     infra/create-vault-schedule.sh --apply    # creates it
#
# Refuses to create a schedule while /greybot/vault/channel_id is unset, for the same reason
# create-recap-schedule.sh refuses a disabled recap: a timer firing into a switched-off
# feature is a second place to look when asking why nothing posted. Runs from the Mac as
# --profile infra. See docs/vault.md for the whole go-live order.
set -euo pipefail

PROFILE="${AWS_PROFILE:-infra}"
REGION=us-east-1
FN=ryangrey-greybot
SCHED_ROLE=ryangrey-greybot-scheduler-role
SCHEDULE=ryangrey-greybot-vault
CRON="cron(30 11 ? * TUE *)"
TZ_NAME=America/New_York

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }

ACCT=$(aws sts get-caller-identity --query Account --output text)
CHANNEL=$(aws ssm get-parameter --name /greybot/vault/channel_id \
  --query Parameter.Value --output text 2>/dev/null || echo "")
case "$CHANNEL" in
  ''|None|*[!0-9]*)
    echo "/greybot/vault/channel_id is unset — refusing to schedule a report with nowhere" >&2
    echo "to post. Create the channel and set the parameter first (docs/vault.md)." >&2
    exit 1 ;;
esac

if aws scheduler get-schedule --name "$SCHEDULE" >/dev/null 2>&1; then
  echo "$SCHEDULE already exists; nothing to do."
  exit 0
fi

echo "Would create $SCHEDULE: $CRON in $TZ_NAME -> $FN {\"mode\":\"vault\"}"
[ "${1:-}" = "--apply" ] || { echo "preview only: re-run with --apply."; exit 0; }

# One retry at most. The week is claimed in DynamoDB before the post, so a retried
# invocation finds the claim and posts nothing.
aws scheduler create-schedule --name "$SCHEDULE" \
  --schedule-expression "$CRON" \
  --schedule-expression-timezone "$TZ_NAME" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target "{\"Arn\":\"arn:aws:lambda:$REGION:$ACCT:function:$FN\",
             \"RoleArn\":\"arn:aws:iam::$ACCT:role/$SCHED_ROLE\",
             \"Input\":\"{\\\"mode\\\":\\\"vault\\\"}\",
             \"RetryPolicy\":{\"MaximumRetryAttempts\":1}}"
aws scheduler get-schedule --name "$SCHEDULE" \
  --query '{expression:ScheduleExpression,timezone:ScheduleExpressionTimezone,state:State}'
