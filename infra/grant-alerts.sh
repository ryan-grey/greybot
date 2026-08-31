#!/usr/bin/env bash
# Let greyBot mail Ryan when Discord throws it out.
#
#   bash infra/grant-alerts.sh        # from the Mac, as `aws --profile infra`
#
# The pipeline already exists and this does not rebuild it:
#
#   greyBot Lambda  --sns:Publish-->  ryangrey-dev-alerts
#                                       -> ryangrey-alert-forwarder
#                                         -> SES, from alerts@ryangrey.dev (DKIM-signed)
#                                           -> rgrey.web@gmail.com
#
# Built by ryangrey.dev/infra/setup-ses-alerts.sh. The forwarder already falls through to a
# plain message body for anything that is not a CloudWatch alarm, so greyBot publishing a
# subject and a block of text needs no change on that side. Reusing it is also why this does
# NOT wire SES into the greybot role directly: a second sender would mean a second
# reputation, a second set of DNS records, and a second thing to debug the next time mail
# goes missing.
#
# NOTE: the direct SNS-to-email subscription is the thing that never worked for the Gmail
# address — four subscribe attempts, zero arrivals. The Lambda forwarder is the only
# delivery path, and it is verified working. Publish here, never subscribe.
#
# ---------------------------------------------------------------------------
# REQUIRES the one-time bootstrap: ryangrey.dev/infra/bootstrap-greybot-iam.sh
#
# ryangrey-greybot-role predates /ryangrey-app/ and sits at path "/", so until that script
# runs, iam:PutRolePolicy on it is an implicitDeny for ryangrey-infra AND for ryan-cli.
# This script detects that and says so rather than failing with a raw AccessDenied.
# ---------------------------------------------------------------------------
#
# Two phases, in this order, because the order is what makes it safe:
#
#   1. Create /greybot/alerts/sns_topic_arn. Harmless on its own — nothing can read it yet,
#      and config.py reads an ungranted optional name as its default, which is "no alerts".
#   2. Replace the inline role policy with one that adds sns:Publish on that one topic and
#      read access to the new parameter.
#
# The policy is the WHOLE policy, not a patch: put-role-policy replaces rather than merges.
# Everything grant-recap-config.sh granted is reproduced verbatim. Dropping any of it would
# be a silent regression — the announcer would keep working and something else would quietly
# stop.
#
# ORDER MATTERS relative to the deploy, too. config.py now asks for the new parameter in the
# same GetParameters chunk as team/prog_overlap_low and team/prog_tag, and SSM denies the
# WHOLE call if one name in it is not granted — so deploying before this runs would silently
# pin those two to their defaults. Run this first, then scripts/deploy.sh.
#
# Idempotent: safe to re-run.
set -euo pipefail

PROFILE="${PROFILE:-infra}"
AWS() { command aws --profile "$PROFILE" "$@"; }

REGION=us-east-1
TABLE=ryangrey-greybot
ROLE=ryangrey-greybot-role
FN=ryangrey-greybot
PARAM=/greybot/alerts/sns_topic_arn
ACCT="${ACCT:-$(AWS sts get-caller-identity --query Account --output text)}"
TOPIC="arn:aws:sns:$REGION:$ACCT:ryangrey-dev-alerts"

echo "Profile: $PROFILE"
echo "Account: $ACCT"
echo "Topic:   $TOPIC"
echo

# ---------------------------------------------------------------- 0. can we?
# Checked up front with a simulate rather than discovered halfway through as an
# AccessDenied, so a missing bootstrap reads as a missing step and not as a broken script.
DECISION=$(AWS iam simulate-principal-policy \
  --policy-source-arn "arn:aws:iam::$ACCT:role/ryangrey-infra" \
  --action-names iam:PutRolePolicy \
  --resource-arns "arn:aws:iam::$ACCT:role/$ROLE" \
  --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null || echo unknown)
if [ "$DECISION" != "allowed" ]; then
  cat <<EOF
==> [!!] $PROFILE cannot write $ROLE's policy (simulate says: $DECISION)

    The role sits at path "/" and ryangrey-infra's IAM write is scoped to
    /ryangrey-app/*. That gap closes once, in CloudShell as admin:

      ryangrey.dev/infra/bootstrap-greybot-iam.sh

    Then re-run this script. Nothing here has changed anything yet.
EOF
  exit 1
fi
echo "==> [ok] $PROFILE may write $ROLE's policy"

# The SNS topic cannot be read from this profile — sns is deliberately absent from
# ryangrey-infra's policy — so its existence is taken on trust here. It was verified when
# the bootstrap ran, and a publish to a missing topic surfaces as health_alert_undeliverable
# in the logs rather than as a broken poll.

# ---------------------------------------------------------------- 1. parameter
#
# Create-if-absent, not --overwrite. If this name already holds a different topic that was a
# deliberate choice, and a re-run must not quietly undo it.
echo
echo "==> Parameter"
if AWS ssm get-parameter --name "$PARAM" --region $REGION >/dev/null 2>&1; then
  echo "    [--] $PARAM already exists — left alone"
else
  AWS ssm put-parameter --region $REGION --name "$PARAM" --type String --value "$TOPIC" \
    --description "SNS topic greyBot publishes Discord health alerts to. Unset = no alerts." \
    >/dev/null
  echo "    [ok] $PARAM = $TOPIC"
fi

# ---------------------------------------------------------------- 2. role policy
echo
echo "==> Role policy"
SSM_KEY=$(AWS kms describe-key --key-id alias/aws/ssm --region $REGION \
  --query KeyMetadata.Arn --output text)

P="arn:aws:ssm:$REGION:$ACCT:parameter/greybot"
cat > /tmp/greybot-policy.json <<J
{"Version":"2012-10-17","Statement":[
 {"Sid":"Logs","Effect":"Allow",
  "Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
  "Resource":"arn:aws:logs:$REGION:$ACCT:log-group:/aws/lambda/$FN:*"},
 {"Sid":"StateTableNoScanNoDelete","Effect":"Allow",
  "Action":["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem"],
  "Resource":"arn:aws:dynamodb:$REGION:$ACCT:table/$TABLE"},
 {"Sid":"ReadOwnConfigAndSecrets","Effect":"Allow",
  "Action":["ssm:GetParameter","ssm:GetParameters"],
  "Resource":["$P/wcl/client_id","$P/wcl/client_secret",
              "$P/discord/webhook_url","$P/discord/prog_role_id",
              "$P/discord/bot_token","$P/discord/public_key","$P/discord/guild_id",
              "$P/guild/name","$P/guild/realm","$P/guild/region",
              "$P/blizzard/client_id","$P/blizzard/client_secret",
              "$P/recap/enabled","$P/recap/show_worst_parse","$P/recap/schedule",
              "$P/team/roster_min_first_kill_pct",
              "$P/team/prog_overlap_high","$P/team/prog_overlap_low",
              "$P/team/prog_tag","$P/alerts/sns_topic_arn"]},
 {"Sid":"DecryptThoseSecretsOnly","Effect":"Allow","Action":"kms:Decrypt",
  "Resource":"$SSM_KEY",
  "Condition":{"StringEquals":{"kms:ViaService":"ssm.$REGION.amazonaws.com"}}},
 {"Sid":"DeferSlowInteractionsToItself","Effect":"Allow","Action":"lambda:InvokeFunction",
  "Resource":"arn:aws:lambda:$REGION:$ACCT:function:$FN"},
 {"Sid":"PublishHealthAlertsToOneTopic","Effect":"Allow","Action":"sns:Publish",
  "Resource":"$TOPIC"}]}
J
AWS iam put-role-policy --role-name $ROLE --policy-name greybot-runtime \
  --policy-document file:///tmp/greybot-policy.json

echo "    [ok] greybot-runtime replaced"
echo
AWS iam get-role-policy --role-name $ROLE --policy-name greybot-runtime \
  --query 'PolicyDocument.Statement[].Sid' --output table

cat <<EOF

Next, from the repo root:

  scripts/deploy.sh

then prove the whole path with a mail that needs nothing to actually be broken:

  aws --profile $PROFILE lambda invoke --function-name $FN --region $REGION \\
    --cli-binary-format raw-in-base64-out \\
    --payload '{"admin":"health","notify":true}' /dev/stdout

Expect every probe "ok", and an email from alerts@ryangrey.dev titled
"greyBot health check: ok". If the invoke says ok but no mail arrives, the break is in the
forwarder, not in greyBot:

  aws --profile $PROFILE logs tail /aws/lambda/ryangrey-alert-forwarder --since 10m --region $REGION
EOF
