#!/usr/bin/env bash
# Let the deploy user read and write greyBot's CONFIG parameters -- but not its secrets.
#
# Run in CloudShell as an admin. ryan-cli cannot edit its own policies, which is the point.
#
# The split is deliberate. The six parameters below are configuration: a guild name, a
# realm slug, a Discord role id, two OAuth client ids. Losing any of them costs nothing.
# The three left out -- the Discord webhook URL and the two client secrets -- are
# credentials: anyone holding the webhook can post to #bots as greyBot. Those stay
# reachable only from an admin session and from the Lambda's own execution role.
#
# Note what this does NOT solve: writing a secret still means the secret has to reach
# whoever is typing the command. For those, the Parameter Store console is the better tool
# regardless of what IAM allows.
#
# Additive: a new inline policy alongside whatever ryan-cli already has.
set -euo pipefail

ACCT="${ACCT:?export ACCT=<your 12-digit account id> first}"
REGION=us-east-1
USER_NAME="${USER_NAME:-ryan-cli}"

cat > /tmp/cli-config-params.json <<J
{"Version":"2012-10-17","Statement":[
 {"Sid":"ReadWriteGreybotConfigNotSecrets","Effect":"Allow",
  "Action":["ssm:GetParameter","ssm:GetParameters","ssm:PutParameter",
            "ssm:DescribeParameters"],
  "Resource":["arn:aws:ssm:$REGION:$ACCT:parameter/greybot/wcl/client_id",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/blizzard/client_id",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/discord/prog_role_id",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/guild/name",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/guild/realm",
              "arn:aws:ssm:$REGION:$ACCT:parameter/greybot/guild/region"]}]}
J

aws iam put-user-policy --user-name "$USER_NAME" \
  --policy-name greybot-config-params \
  --policy-document file:///tmp/cli-config-params.json

echo "Granted. $USER_NAME can now read and write these, and only these:"
aws iam get-user-policy --user-name "$USER_NAME" --policy-name greybot-config-params \
  --query 'PolicyDocument.Statement[0].Resource[]' --output table
echo
echo "Still out of reach from the CLI, by design:"
echo "  /greybot/wcl/client_secret"
echo "  /greybot/blizzard/client_secret"
echo "  /greybot/discord/webhook_url"
