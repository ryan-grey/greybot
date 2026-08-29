#!/usr/bin/env bash
# Add the two Blizzard parameters to the Lambda role's existing policy.
#
# Needed only because infra/iam-setup.sh already ran before boss art existed. On a fresh
# setup iam-setup.sh covers these and this script is unnecessary.
#
# It rewrites the whole greybot-runtime inline policy rather than appending, because
# put-role-policy replaces the document wholesale -- appending is not an operation IAM has.
set -euo pipefail

ACCT="${ACCT:?export ACCT=<your 12-digit account id> first}"
REGION=us-east-1
TABLE=ryangrey-greybot
ROLE=ryangrey-greybot-role
FN=ryangrey-greybot

SSM_KEY=$(aws kms describe-key --key-id alias/aws/ssm --region $REGION \
  --query KeyMetadata.Arn --output text)

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
  "Resource":["arn:aws:ssm:$REGION:$ACCT:parameter/greybot/wcl/client_id",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/wcl/client_secret",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/discord/webhook_url",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/discord/prog_role_id",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/guild/name",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/guild/realm",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/guild/region",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/blizzard/client_id",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/blizzard/client_secret"]},
 {"Sid":"DecryptThoseSecretsOnly","Effect":"Allow","Action":"kms:Decrypt",
  "Resource":"$SSM_KEY",
  "Condition":{"StringEquals":{"kms:ViaService":"ssm.$REGION.amazonaws.com"}}}]}
J
aws iam put-role-policy --role-name $ROLE --policy-name greybot-runtime \
  --policy-document file:///tmp/policy.json

echo "Policy updated. The role can now read the two Blizzard parameters."
aws iam get-role-policy --role-name $ROLE --policy-name greybot-runtime \
  --query 'PolicyDocument.Statement[?Sid==`ReadOwnConfigAndSecrets`].Resource[]' --output table
