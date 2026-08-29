#!/usr/bin/env bash
# The second EventBridge schedule: the morning-after raid recap.
#
# Scrambled raids Tuesday and Thursday, 9pm to midnight Eastern, so the recap fires
# Wednesday and Friday at 10am Eastern. Two cards a week, each covering one night --
# despite the name, this is not a weekly digest, and the brief's own contents ("top 3
# damage for the night", "bosses killed that night") describe one raid night.
#
# NOTE THE TIMEZONE ARGUMENT. It is the whole reason this schedule is a cron rather than
# a rate(), and it is what the poller's rate(15 minutes) never needed: a bare UTC cron for
# 10am Eastern is 14:00 in summer and 15:00 in winter, so it silently drifts an hour every
# November and every March and has to be edited by hand twice a year. Scheduler will keep
# 10am at 10am if you tell it which 10am you meant.
#
# Both the expression and whether this runs at all come from SSM rather than from this
# file, so the recorded configuration and the thing actually firing cannot disagree.
#
# Run in CloudShell as an admin, AFTER infra/grant-recap-config.sh. Export ACCT first.
set -euo pipefail

ACCT="${ACCT:?export ACCT=<your 12-digit account id> first}"
REGION=us-east-1
FN=ryangrey-greybot
SCHED_ROLE=ryangrey-greybot-scheduler-role
SCHEDULE=ryangrey-greybot-recap
TZ_NAME=America/New_York

# Same refusal as create-schedule.sh: Scheduler validates its target at creation time, so
# a missing function yields a less obvious error than simply saying so here.
aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1 || {
  echo "Lambda $FN does not exist yet. Run scripts/deploy.sh first." >&2
  exit 1
}

ENABLED=$(aws ssm get-parameter --name /greybot/recap/enabled --region $REGION \
  --query Parameter.Value --output text 2>/dev/null || echo "missing")
case "$(printf '%s' "$ENABLED" | tr '[:upper:]' '[:lower:]')" in
  true|1|yes|on) ;;
  *)
    # Deliberate, and the most useful thing this script does. A schedule that fires into a
    # handler which then reads enabled=false and returns is not harmless: it is a timer
    # nobody remembers creating, invoking a function twice a week for no reason, and it
    # makes "why did the recap not post" a two-place question instead of a one-place one.
    echo "/greybot/recap/enabled is '$ENABLED' — refusing to create a schedule for a" >&2
    echo "feature that is switched off. Turn it on once the recap card has been previewed:" >&2
    echo "  aws ssm put-parameter --name /greybot/recap/enabled --value true \\" >&2
    echo "    --type String --overwrite --region $REGION" >&2
    exit 1 ;;
esac

CRON=$(aws ssm get-parameter --name /greybot/recap/schedule --region $REGION \
  --query Parameter.Value --output text)
[ -n "$CRON" ] && [ "$CRON" != "None" ] || {
  echo "/greybot/recap/schedule is empty — nothing to create." >&2; exit 1; }

echo "Creating $SCHEDULE: $CRON in $TZ_NAME"

# RetryPolicy is 2, matching the poller. Retries are safe here for the same reason they are
# safe there: the recap claims its night in DynamoDB before it posts, so a retried
# invocation loses the conditional write and posts nothing.
aws scheduler create-schedule --region $REGION --name $SCHEDULE \
  --schedule-expression "$CRON" \
  --schedule-expression-timezone "$TZ_NAME" \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target "{\"Arn\":\"arn:aws:lambda:$REGION:$ACCT:function:$FN\",
             \"RoleArn\":\"arn:aws:iam::$ACCT:role/$SCHED_ROLE\",
             \"Input\":\"{\\\"mode\\\":\\\"recap\\\"}\",
             \"RetryPolicy\":{\"MaximumRetryAttempts\":2}}"

echo
echo "Schedule $SCHEDULE created."
echo "It invokes the SAME function as the poller, with {\"mode\":\"recap\"} — one Lambda,"
echo "two schedules, not a parallel stack."
echo
echo "Next fire:"
aws scheduler get-schedule --name $SCHEDULE --region $REGION \
  --query '{expression:ScheduleExpression,timezone:ScheduleExpressionTimezone,state:State}'
