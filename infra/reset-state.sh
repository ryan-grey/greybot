#!/usr/bin/env bash
# DESTRUCTIVE. Deletes greyBot's remembered state for one guild so the next invocation
# bootstraps again from scratch.
#
#   bash infra/reset-state.sh          # from the Mac, as `aws --profile infra`
#
# NOT CloudShell, and not admin. That instruction was true when this was written and stopped
# being true on 2026-08-31: ryangrey-infra holds dynamodb:* on the account's tables, so the
# human path to DeleteItem is a profile on this machine. Verified with
# simulate-principal-policy rather than assumed.
#
# The distinction that matters, and the reason this is not a hole:
#
#   ryangrey-infra         DeleteItem  ALLOWED       a person, deliberately, at a prompt
#   ryangrey-greybot-role  DeleteItem  implicitDeny  the bot, always, no exceptions
#
# The runtime role still cannot delete anything and must never be given the ability. That is
# what makes "announce exactly once" a property of the permissions and not just of the code:
# nothing running unattended can erase the record of what was already announced. Moving the
# operator's path off CloudShell does not touch that.
#
# The only reason to run this is a bad seed. Deleting the state does NOT delete anything in
# Discord; it means the next run re-reads Raider.IO and the log history and writes a fresh
# set of already-killed bosses, announcing nothing. Running it while the schedule is live is
# safe for the same reason -- the next run seeds rather than announces -- but disable the
# schedule first if you want to watch the reseed by hand.
#
# What goes with it, which is easy to forget: everything under this guild's partition key,
# including the two monitoring items added on 2026-08-31.
#
#   BOOTSTRAP   the marker that stops run one announcing
#   TIER#...    announced sets, seed sizes, baselines, AOTC flags
#   KILL#...    first-kill rosters
#   ROSTER#...  the derived prog-team roster
#   RECAPS      which nights have been posted
#   PROGRESS    the /progress snapshot
#   HEALTH      Discord standing. Cleared, the next check records a fresh baseline
#               SILENTLY -- the first-run guard means no "recovered" email fires
#   SOURCE      the log-source blindness streak, reset to zero
#
# Boss art under ART#GLOBAL is a different partition key and is NOT touched. It is a cache
# of Blizzard lookups shared across guilds and there is no reason to make the next run pay
# for them again.
set -euo pipefail

PROFILE="${PROFILE:-infra}"
AWS() { command aws --profile "$PROFILE" "$@"; }

REGION=us-east-1
TABLE=ryangrey-greybot
PK="${PK:-GUILD#us#proudmoore#scrambled}"
ACCT="$(AWS sts get-caller-identity --query Account --output text)"

echo "Profile: $PROFILE"
echo "Table:   $TABLE"
echo "Key:     $PK"
echo

# Checked up front with a simulate rather than discovered halfway through a deletion loop
# as an AccessDenied, which would leave the state half-erased -- the one outcome worse than
# either keeping it or clearing it.
DECISION=$(AWS iam simulate-principal-policy \
  --policy-source-arn "arn:aws:iam::$ACCT:role/ryangrey-infra" \
  --action-names dynamodb:DeleteItem \
  --resource-arns "arn:aws:dynamodb:$REGION:$ACCT:table/$TABLE" \
  --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null || echo unknown)
if [ "$DECISION" != "allowed" ]; then
  echo "[!!] $PROFILE cannot delete from $TABLE (simulate says: $DECISION)."
  echo "     Nothing has been touched. Check you are on the infra profile:"
  echo "       aws --profile infra sts get-caller-identity"
  exit 1
fi

# Read the key list ONCE and delete from that same list. Querying twice -- once to show and
# once to delete -- means the operator confirms one set of items and the script erases
# whatever the second query happened to return.
ITEMS="$(AWS dynamodb query --table-name "$TABLE" --region "$REGION" \
  --key-condition-expression 'pk = :p' \
  --expression-attribute-values "{\":p\":{\"S\":\"$PK\"}}" \
  --query 'Items[].sk.S' --output text | tr '\t' '\n' | grep -v '^$' || true)"

if [ -z "$ITEMS" ]; then
  echo "No items stored for that key. Nothing to delete."
  exit 0
fi

echo "Items currently stored:"
printf '%s\n' "$ITEMS" | sed 's/^/  /'
echo
printf 'Delete all %d of the above? [y/N] ' "$(printf '%s\n' "$ITEMS" | wc -l | tr -d ' ')"
read -r ANSWER
[ "$ANSWER" = "y" ] || { echo "Nothing deleted."; exit 0; }

printf '%s\n' "$ITEMS" | while read -r SK; do
  [ -n "$SK" ] || continue
  AWS dynamodb delete-item --table-name "$TABLE" --region "$REGION" \
    --key "{\"pk\":{\"S\":\"$PK\"},\"sk\":{\"S\":\"$SK\"}}"
  echo "  deleted $SK"
done

cat <<EOF

State cleared. The next invocation will bootstrap and announce nothing:

  aws --profile $PROFILE lambda invoke --function-name ryangrey-greybot --region $REGION /dev/stdout

Expect {"bootstrapped": true, "announced": 0}. If that first run announces anything,
disable the schedule immediately:

  aws --profile $PROFILE scheduler update-schedule --name ryangrey-greybot-poll --state DISABLED
EOF
