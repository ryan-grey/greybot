#!/usr/bin/env bash
# One-time admin setup. Run this in CloudShell as an admin, NOT as the deploy user.
#
# The deploy identity has no IAM write and no EventBridge Scheduler write, so everything
# here is created once by a human with broader rights and then left alone.
# scripts/deploy.sh verifies these exist and complains if they drift; it never creates them.
#
# This is step 1 of 2 in CloudShell. The EventBridge schedule is deliberately NOT here:
# Scheduler validates its target at creation time, so the Lambda must exist first, and the
# schedule should not start firing until the first invocation is confirmed to have seeded
# rather than announced. It lives in infra/create-schedule.sh.
#
# The seven SSM parameters are ALREADY PROVISIONED and are deliberately not created here --
# this script only grants read access to them. They are, in us-east-1:
#
#   /greybot/wcl/client_id         String
#   /greybot/wcl/client_secret     SecureString
#   /greybot/discord/webhook_url   SecureString
#   /greybot/discord/prog_role_id  String
#   /greybot/guild/name            String
#   /greybot/guild/realm           String        (slug: lowercase, hyphenated)
#   /greybot/guild/region          String
#   /greybot/blizzard/client_id    String        (optional -- per-boss art)
#   /greybot/blizzard/client_secret SecureString (optional -- per-boss art)
#
# Export ACCT before running:  export ACCT=123456789012
set -euo pipefail

# A literal placeholder here would be substituted into IAM policy ARNs and create a role
# that grants access to nothing, which fails much later and much less clearly. Fail now.
ACCT="${ACCT:?export ACCT=<your 12-digit account id> first}"
REGION=us-east-1
TABLE=ryangrey-greybot
ROLE=ryangrey-greybot-role
SCHED_ROLE=ryangrey-greybot-scheduler-role
FN=ryangrey-greybot
SCHEDULE=ryangrey-greybot-poll

# --- DynamoDB: one table, composite key, on-demand ----------------------------
# Tier rollover needs no migration: a new raid slug is simply a new sort key.
aws dynamodb create-table --region $REGION --table-name $TABLE \
  --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
  --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST
aws dynamodb wait table-exists --table-name $TABLE --region $REGION

SSM_KEY=$(aws kms describe-key --key-id alias/aws/ssm --region $REGION \
  --query KeyMetadata.Arn --output text)

# --- Lambda execution role: no wildcards, no Scan, no Delete* -----------------
cat > /tmp/trust.json <<'J'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
J
aws iam create-role --role-name $ROLE --assume-role-policy-document file:///tmp/trust.json

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

# Seven parameter ARNs written out rather than /greybot/* on purpose. A wildcard would also
# grant every parameter added under that prefix later, which is exactly the kind of grant
# that quietly widens. Note the ARN form has no second slash: .../parameter/greybot/... for
# a parameter named /greybot/... -- getting that wrong yields an AccessDenied that looks
# like the parameter is missing.
#
# SecureString needs BOTH ssm:GetParameters and kms:Decrypt. Granting only the first fails
# at read time with an AccessDenied that names KMS, not SSM, which is a confusing place to
# start debugging. The ViaService condition means this role cannot use the key for anything
# except reading these parameters back through SSM.

# --- Scheduler -> Lambda ------------------------------------------------------
cat > /tmp/sched-trust.json <<J
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole",
 "Condition":{"StringEquals":{"aws:SourceAccount":"$ACCT"}}}]}
J
aws iam create-role --role-name $SCHED_ROLE \
  --assume-role-policy-document file:///tmp/sched-trust.json

cat > /tmp/sched-policy.json <<J
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"lambda:InvokeFunction",
 "Resource":"arn:aws:lambda:$REGION:$ACCT:function:$FN"}]}
J
aws iam put-role-policy --role-name $SCHED_ROLE --policy-name invoke-greybot \
  --policy-document file:///tmp/sched-policy.json

echo
echo "Roles and table created. Next: scripts/deploy.sh from the repo, then verify the"
echo "first invocation SEEDED, then infra/create-schedule.sh back here."
