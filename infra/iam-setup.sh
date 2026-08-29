#!/usr/bin/env bash
# One-time admin setup. Run this in CloudShell as an admin, NOT as the deploy user.
#
# The deploy identity has no IAM write, no SSM write and no EventBridge Scheduler write,
# so everything here is created once by a human with broader rights and then left alone.
# scripts/deploy.sh verifies these exist and complains if they drift; it never creates them.
#
# Export ACCT before running:  export ACCT=123456789012
set -euo pipefail

# A literal placeholder here would be substituted into IAM policy ARNs and create a role
# that grants access to nothing, which fails much later and much less clearly. Fail now.
ACCT="${ACCT:?export ACCT=<your 12-digit account id> first}"
REGION=us-east-1
TABLE=ryangrey-scrambled
ROLE=ryangrey-scrambled-role
SCHED_ROLE=ryangrey-scrambled-scheduler-role
FN=ryangrey-scrambled-bot
SCHEDULE=ryangrey-scrambled-poll

# --- DynamoDB: one table, composite key, on-demand ----------------------------
# Tier rollover needs no migration: a new raid slug is simply a new sort key.
aws dynamodb create-table --region $REGION --table-name $TABLE \
  --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
  --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST
aws dynamodb wait table-exists --table-name $TABLE --region $REGION

# --- Secrets. SecureString, so they are never in the Lambda's environment ------
# A Discord webhook URL is a post-anything-to-#bots credential, not a config value:
# anyone who can read it can post as the bot. Same for the Warcraft Logs client secret.
# Create the WCL client first at https://www.warcraftlogs.com/api/clients/
read -rsp 'Warcraft Logs client_id: '     WCL_ID;     echo
read -rsp 'Warcraft Logs client_secret: ' WCL_SECRET; echo
read -rsp 'Discord #bots webhook URL: '   HOOK;       echo

aws ssm put-parameter --region $REGION --name /scrambled/wcl/client_id \
  --type SecureString --value "$WCL_ID" --overwrite
aws ssm put-parameter --region $REGION --name /scrambled/wcl/client_secret \
  --type SecureString --value "$WCL_SECRET" --overwrite
aws ssm put-parameter --region $REGION --name /scrambled/discord/webhook_url \
  --type SecureString --value "$HOOK" --overwrite
unset WCL_ID WCL_SECRET HOOK

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
 {"Sid":"ReadOwnSecrets","Effect":"Allow",
  "Action":["ssm:GetParameters"],
  "Resource":["arn:aws:ssm:$REGION:$ACCT:parameter/scrambled/wcl/client_id",
              "arn:aws:ssm:$REGION:$ACCT:parameter/scrambled/wcl/client_secret",
              "arn:aws:ssm:$REGION:$ACCT:parameter/scrambled/discord/webhook_url"]},
 {"Sid":"DecryptThoseSecretsOnly","Effect":"Allow","Action":"kms:Decrypt",
  "Resource":"$SSM_KEY",
  "Condition":{"StringEquals":{"kms:ViaService":"ssm.$REGION.amazonaws.com"}}}]}
J
aws iam put-role-policy --role-name $ROLE --policy-name scrambled-runtime \
  --policy-document file:///tmp/policy.json

# SecureString needs BOTH ssm:GetParameters and kms:Decrypt. Granting only the first
# fails at read time with an AccessDenied that names KMS, not SSM, which is a confusing
# place to start debugging. The ViaService condition means this role cannot use the key
# for anything except reading these parameters back through SSM.

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
aws iam put-role-policy --role-name $SCHED_ROLE --policy-name invoke-scrambled-bot \
  --policy-document file:///tmp/sched-policy.json

# Deploy the function first (scripts/deploy.sh), then create the schedule -- Scheduler
# validates the target at creation time and refuses a function that does not exist yet.
aws scheduler create-schedule --region $REGION --name $SCHEDULE \
  --schedule-expression 'rate(15 minutes)' \
  --flexible-time-window '{"Mode":"OFF"}' \
  --target "{\"Arn\":\"arn:aws:lambda:$REGION:$ACCT:function:$FN\",
             \"RoleArn\":\"arn:aws:iam::$ACCT:role/$SCHED_ROLE\",
             \"RetryPolicy\":{\"MaximumRetryAttempts\":2}}"

echo "Done. Now run scripts/deploy.sh from the repo as the deploy user."
