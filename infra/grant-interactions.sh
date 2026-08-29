#!/usr/bin/env bash
# Widen the Lambda role for slash commands. Supersedes infra/grant-blizzard-read.sh.
#
# Two additions beyond the Blizzard grant:
#   * the three new Discord parameters (bot token, public key, guild id)
#   * lambda:InvokeFunction on ITSELF
#
# The self-invoke is not incidental. A Lambda cannot answer Discord with "deferred" and
# then keep working -- execution stops when the handler returns -- so deferring means
# responding immediately and asynchronously invoking a second copy to do the slow part and
# PATCH the follow-up. Without this permission the fallback path for a cold start silently
# does not exist.
#
# Run in CloudShell as an admin. Export ACCT first.
set -euo pipefail

ACCT="${ACCT:?export ACCT=<your 12-digit account id> first}"
REGION=us-east-1
TABLE=ryangrey-greybot
ROLE=ryangrey-greybot-role
FN=ryangrey-greybot

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
              "$P/blizzard/client_id","$P/blizzard/client_secret"]},
 {"Sid":"DecryptThoseSecretsOnly","Effect":"Allow","Action":"kms:Decrypt",
  "Resource":"$SSM_KEY",
  "Condition":{"StringEquals":{"kms:ViaService":"ssm.$REGION.amazonaws.com"}}},
 {"Sid":"DeferSlowInteractionsToItself","Effect":"Allow","Action":"lambda:InvokeFunction",
  "Resource":"arn:aws:lambda:$REGION:$ACCT:function:$FN"}]}
J
aws iam put-role-policy --role-name $ROLE --policy-name greybot-runtime \
  --policy-document file:///tmp/policy.json

echo "Role updated. Parameters it can now read:"
aws iam get-role-policy --role-name $ROLE --policy-name greybot-runtime \
  --query 'PolicyDocument.Statement[?Sid==`ReadOwnConfigAndSecrets`].Resource[]' --output table
