#!/bin/sh
set -eu
test "$(id -u)" = 0 || exit 1
release=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "$release" in
  /volume1/docker/greybot/releases/*) ;;
  *) exit 1 ;;
esac
test ! -L /volume1/docker/greybot/state
test ! -L /volume1/docker/greybot/private
chmod 0700 /volume1/docker/greybot/state /volume1/docker/greybot/private
test "$(stat -c %a /volume1/docker/greybot/state)" = 700
umask 022
docker run --rm --network none --read-only --entrypoint python greybot-control:local \
  -c 'import greybot_control.web, greybot_control.worker; print("greyBot image imports passed")'
{
  docker image inspect greybot-control:local --format '{{.Id}}'
  stat -c '%a %u:%g %n' /volume1/docker/greybot/state /volume1/docker/greybot/private
  echo 'Non-root imports passed; private directory permissions verified; services not started.'
} > "$release/build-verification.txt"
chmod 0644 "$release/build-verification.txt"
cat "$release/build-verification.txt"
