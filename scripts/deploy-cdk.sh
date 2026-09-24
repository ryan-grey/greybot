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
  PY="$ROOT/.venv/bin/python"
else
  echo "!!  no .venv — skipping the self-test's PyNaCl signature checks" >&2
  PY=python3
fi
echo "==> Self-test"
"$PY" "$ROOT/scripts/selftest.py" >/dev/null
echo "    all checks passed"

# The feature suites the self-test does not cover. Each is its own script with its own
# exit code; any one failing stops the deploy, and its output is shown only then.
echo "==> Feature tests"
for t in "$ROOT"/scripts/test_*.py; do
  if ! out="$("$PY" "$t" 2>&1)"; then
    printf '%s\n' "$out" >&2
    echo "!!  $(basename "$t") failed" >&2
    exit 1
  fi
done
echo "    all passed"

# The NAS control service's suite, in its own venv: its pins (discord.py, fastapi, the
# voice receiver) have nothing to do with the Lambda package. Built on first use, with
# voice-recv --no-deps exactly as the Dockerfile installs it.
CONTROL="$ROOT/control"
CVENV="$CONTROL/.venv"
if [ ! -x "$CVENV/bin/python" ]; then
  echo "==> Creating control/.venv for the control service's tests"
  python3 -m venv "$CVENV"
  "$CVENV/bin/pip" install --quiet -r "$CONTROL/requirements.txt" \
    -r "$CONTROL/requirements-reader.txt"
  "$CVENV/bin/pip" install --quiet --no-deps -r "$CONTROL/requirements-voice.txt"
fi
echo "==> Control service tests"
if ! out="$(cd "$CONTROL" && "$CVENV/bin/python" -m unittest discover -s tests -p 'test_*.py' 2>&1)"; then
  printf '%s\n' "$out" >&2
  echo "!!  control service tests failed" >&2
  exit 1
fi
echo "    $(printf '%s\n' "$out" | grep -E '^Ran ')"

echo "==> Packaging"
"$ROOT/scripts/build-lambda.sh"

echo "==> Deploying greybot-$STAGE (profile: $PROFILE)"
cd "$ROOT/cdk"
AWS_PROFILE="$PROFILE" npx --yes "cdk@$CDK_VERSION" deploy "greybot-$STAGE" \
  --require-approval never
