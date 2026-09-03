#!/usr/bin/env bash
# Deploy one stage. The ONLY supported way to ship this bot.
#
#     scripts/deploy-cdk.sh dev
#     scripts/deploy-cdk.sh prod
#
# WHY A WRAPPER. The deploy was three things chained on one line -- cd into cdk/, an
# AWS_PROFILE prefix, then npx cdk -- and every one of them is a way to get it wrong:
# forget the build and you ship src/ without PyNaCl, forget the profile and you deploy
# with whatever credentials happen to be default. It also cannot be expressed as a single
# permission rule, because a compound command starting with `cd` does not match a rule
# written against `npx`.
#
# One script, one name, one rule. The gate below is the part that matters: the package is
# rebuilt from source on every deploy, so build/lambda can never be stale relative to src/.
set -euo pipefail

STAGE="${1:-}"
case "$STAGE" in
  dev|prod) ;;
  *) echo "usage: scripts/deploy-cdk.sh <dev|prod>" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${AWS_PROFILE:-infra}"
CDK_VERSION="${CDK_VERSION:-2.1139.0}"

# The self-test gates the deploy, exactly as the pre-CDK scripts/deploy.sh did. Losing that
# gate in the CDK cutover is how three separate defects reached prod at once.
if [ -x "$ROOT/.venv/bin/python" ]; then
  echo "==> Self-test"
  "$ROOT/.venv/bin/python" "$ROOT/scripts/selftest.py" >/dev/null
  echo "    all checks passed"
else
  echo "!!  no .venv — skipping the self-test's PyNaCl signature checks" >&2
  python3 "$ROOT/scripts/selftest.py" >/dev/null
fi

echo "==> Packaging"
"$ROOT/scripts/build-lambda.sh"

echo "==> Deploying greybot-$STAGE (profile: $PROFILE)"
cd "$ROOT/cdk"
AWS_PROFILE="$PROFILE" npx --yes "cdk@$CDK_VERSION" deploy "greybot-$STAGE" \
  --require-approval never
