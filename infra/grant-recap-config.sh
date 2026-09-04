#!/usr/bin/env bash
# Widen the Lambda role for the weekly recap, and create the parameters it reads.
# Supersedes infra/grant-interactions.sh.
#
# Two phases, in this order, because the order is what makes it safe:
#
#   1. Create the six new parameters. Harmless on their own -- nothing can read them yet,
#      and /greybot/recap/enabled is created as FALSE, so the recap stays dark.
#   2. Replace the inline role policy with one that also grants those six names.
#
# The policy document below is the WHOLE policy, not a patch, because put-role-policy
# replaces rather than merges. Everything grant-interactions.sh granted is reproduced here
# verbatim -- the Blizzard keys, the three Discord interaction parameters, and the
# self-invoke that /progress depends on. Dropping any of those would be a silent
# regression: the announcer would keep working and the slash command would stop.
#
# Parameter creation is create-if-absent rather than --overwrite. The two overlap
# thresholds are tuning knobs that only get interesting after a few weeks of real reports,
# and a re-run of this script must not quietly reset a value that was tuned by hand.
#
# Run in CloudShell as an admin. Export ACCT first.
set -euo pipefail

ACCT="${ACCT:?export ACCT=<your 12-digit account id> first}"
REGION=us-east-1
TABLE=ryangrey-greybot
ROLE=ryangrey-greybot-role
FN=ryangrey-greybot

# ---------------------------------------------------------------- 1. parameters
#
# name|type|value|description
#
# enabled starts FALSE on purpose. The recap posts a card naming individual raiders into a
# live guild channel, and "it started posting because a parameter appeared" is not an
# acceptable way for that to begin. Flip it by hand once the card has been previewed.
#
# 70 and 35 are a margin, not a majority. Several people raid on both of Scrambled's teams,
# so a B-team report legitimately carries A-team players; a 51% rule would call it prog
# about half the time. Between the two thresholds the bot says nothing.
PARAMS=(
  "/greybot/recap/enabled|String|false|Post the morning-after raid recap at all."
  "/greybot/recap/show_worst_parse|String|false|Include the worst parse. Off by default: parse-shaming starts arguments."
  "/greybot/recap/schedule|String|cron(30 5 ? * WED,FRI *)|Recorded cron for the recap. EventBridge Scheduler is what actually runs it."
  "/greybot/team/roster_min_first_kill_pct|String|50|A player joins the prog roster at this share of the tier's first kills."
  "/greybot/team/prog_overlap_high|String|70|Roster overlap at or above this reads as the prog team."
  "/greybot/team/prog_overlap_low|String|35|Roster overlap at or below this reads as the other team."
)

# prog_tag is GRANTED but deliberately NOT CREATED. Its unset value is the empty string
# and SSM rejects an empty parameter value, so the only way to "create" it would be a
# placeholder like "none" -- which is a string a real Warcraft Logs tag could equal. An
# absent parameter is the honest representation of "no tag configured", and config.py
# already reads a missing optional name as its default.
#
# It still has to appear in the policy above. GetParameters denies the WHOLE call if the
# caller lacks permission on any single name in it, and prog_tag shares a request chunk
# with prog_overlap_low -- so omitting it here would silently pin that threshold to its
# default no matter what the parameter said.

echo "==> Parameters"
for SPEC in "${PARAMS[@]}"; do
  IFS='|' read -r NAME TYPE VALUE DESC <<<"$SPEC"
  if aws ssm get-parameter --name "$NAME" --region $REGION >/dev/null 2>&1; then
    echo "    [--] $NAME already exists — left alone"
  else
    aws ssm put-parameter --region $REGION --name "$NAME" --type "$TYPE" \
      --value "$VALUE" --description "$DESC" >/dev/null
    echo "    [ok] $NAME = $VALUE"
  fi
done

# ---------------------------------------------------------------- 2. role policy
echo
echo "==> Role policy"
SSM_KEY=$(aws kms describe-key --key-id alias/aws/ssm --region $REGION \
  --query KeyMetadata.Arn --output text)

P="arn:aws:ssm:$REGION:$ACCT:parameter/greybot"
cat > /tmp/policy.json <<J
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
              "$P/team/prog_tag"]},
 {"Sid":"DecryptThoseSecretsOnly","Effect":"Allow","Action":"kms:Decrypt",
  "Resource":"$SSM_KEY",
  "Condition":{"StringEquals":{"kms:ViaService":"ssm.$REGION.amazonaws.com"}}},
 {"Sid":"DeferSlowInteractionsToItself","Effect":"Allow","Action":"lambda:InvokeFunction",
  "Resource":"arn:aws:lambda:$REGION:$ACCT:function:$FN"}]}
J
aws iam put-role-policy --role-name $ROLE --policy-name greybot-runtime \
  --policy-document file:///tmp/policy.json

echo "    [ok] greybot-runtime replaced"
echo
echo "Parameters the role can now read:"
aws iam get-role-policy --role-name $ROLE --policy-name greybot-runtime \
  --query 'PolicyDocument.Statement[?Sid==`ReadOwnConfigAndSecrets`].Resource[]' --output table

echo
echo "The recap is configured but OFF. Deploy, then preview a real card with:"
echo "  aws lambda invoke --function-name ryangrey-greybot --region $REGION \\"
echo "    --cli-binary-format raw-in-base64-out \\"
echo "    --payload '{\"mode\":\"recap\",\"dry\":true,\"hours\":48}' /dev/stdout"
echo
echo "The schedule is deliberately NOT created here. See infra/create-recap-schedule.sh,"
echo "which refuses to run until /greybot/recap/enabled is true."
