#!/usr/bin/env bash
# The HTTP endpoint Discord POSTs interactions to. API Gateway HTTP API -> Lambda.
#
# Still no gateway connection and still no server: a slash command is one HTTPS request.
#
# Run in CloudShell as an admin -- the deploy user has no API Gateway write. Idempotent:
# re-running reuses the existing API rather than creating a second one.
set -euo pipefail

ACCT="${ACCT:?export ACCT=<your 12-digit account id> first}"
REGION=us-east-1
FN=ryangrey-greybot
API_NAME=greybot-interactions
ROUTE="POST /interactions"

FN_ARN="arn:aws:lambda:$REGION:$ACCT:function:$FN"
aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null

API_ID="$(aws apigatewayv2 get-apis --region "$REGION" \
  --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)"
if [ -z "$API_ID" ] || [ "$API_ID" = "None" ]; then
  echo "Creating HTTP API $API_NAME"
  API_ID="$(aws apigatewayv2 create-api --region "$REGION" --name "$API_NAME" \
    --protocol-type HTTP --query ApiId --output text)"
else
  echo "Reusing HTTP API $API_NAME ($API_ID)"
fi

INT_ID="$(aws apigatewayv2 get-integrations --region "$REGION" --api-id "$API_ID" \
  --query "Items[?IntegrationUri=='${FN_ARN}'].IntegrationId | [0]" --output text)"
if [ -z "$INT_ID" ] || [ "$INT_ID" = "None" ]; then
  # Payload format 2.0 is what the handler expects: it reads requestContext.http,
  # headers, body and isBase64Encoded.
  INT_ID="$(aws apigatewayv2 create-integration --region "$REGION" --api-id "$API_ID" \
    --integration-type AWS_PROXY --integration-uri "$FN_ARN" \
    --payload-format-version 2.0 --timeout-in-millis 10000 \
    --query IntegrationId --output text)"
fi

EXISTING="$(aws apigatewayv2 get-routes --region "$REGION" --api-id "$API_ID" \
  --query "Items[?RouteKey=='${ROUTE}'].RouteId | [0]" --output text)"
if [ -z "$EXISTING" ] || [ "$EXISTING" = "None" ]; then
  aws apigatewayv2 create-route --region "$REGION" --api-id "$API_ID" \
    --route-key "$ROUTE" --target "integrations/$INT_ID" >/dev/null
fi
# One explicit route and deliberately no $default: an unknown path 404s at the gateway
# instead of reaching the handler.

aws apigatewayv2 create-stage --region "$REGION" --api-id "$API_ID" \
  --stage-name '$default' --auto-deploy >/dev/null 2>&1 || true

aws lambda add-permission --function-name "$FN" --region "$REGION" \
  --statement-id greybot-interactions-invoke \
  --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:$REGION:$ACCT:$API_ID/*/*/interactions" \
  >/dev/null 2>&1 || true

echo
echo "Interactions Endpoint URL — paste this into the Developer Portal:"
echo
echo "  https://${API_ID}.execute-api.${REGION}.amazonaws.com/interactions"
echo
echo "Discord will immediately PING it and refuse to save the URL unless the signature"
echo "check passes, so /greybot/discord/public_key must be set BEFORE you paste it."
