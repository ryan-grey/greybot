#!/usr/bin/env bash
# DESTRUCTIVE. Deletes greyBot's remembered state for one guild so the next invocation
# bootstraps again from scratch.
#
# Run in CloudShell: the Lambda's execution role has no DeleteItem (by design -- nothing in
# normal operation ever deletes), and neither does the deploy user.
#
# The only reason to run this is a bad seed. Deleting the state does NOT delete anything in
# Discord; it means the next run re-reads Raider.IO and the log history and writes a fresh
# set of already-killed bosses, announcing nothing. Running it while the schedule is live
# is safe for the same reason -- the next run seeds rather than announces -- but disable
# the schedule first if you want to watch the reseed by hand.
set -euo pipefail

REGION=us-east-1
TABLE=ryangrey-greybot
PK="${PK:-GUILD#us#proudmoore#scrambled}"

echo "Items currently stored for $PK:"
aws dynamodb query --table-name "$TABLE" --region "$REGION" \
  --key-condition-expression 'pk = :p' \
  --expression-attribute-values "{\":p\":{\"S\":\"$PK\"}}" \
  --query 'Items[].sk.S' --output text | tr '\t' '\n' | sed 's/^/  /'

read -rp 'Delete all of the above? [y/N] ' ANSWER
[ "$ANSWER" = "y" ] || { echo "Nothing deleted."; exit 0; }

aws dynamodb query --table-name "$TABLE" --region "$REGION" \
  --key-condition-expression 'pk = :p' \
  --expression-attribute-values "{\":p\":{\"S\":\"$PK\"}}" \
  --query 'Items[].sk.S' --output text | tr '\t' '\n' | while read -r SK; do
  [ -n "$SK" ] || continue
  aws dynamodb delete-item --table-name "$TABLE" --region "$REGION" \
    --key "{\"pk\":{\"S\":\"$PK\"},\"sk\":{\"S\":\"$SK\"}}"
  echo "  deleted $SK"
done

echo
echo "State cleared. The next invocation will bootstrap and announce nothing:"
echo "  aws lambda invoke --function-name ryangrey-greybot --region $REGION /dev/stdout"
