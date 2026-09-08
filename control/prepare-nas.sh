#!/bin/sh
# Build the isolated image and initialize its private state; start no services.
set -eu
test "$(id -u)" = 0 || { echo 'Run with sudo on the NAS.' >&2; exit 1; }
release=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "$release" in
  /volume1/docker/greybot/releases/*) ;;
  *) echo 'Unexpected release directory; stopping.' >&2; exit 1 ;;
esac
docker build --tag greybot-control:local --file "$release/control/Dockerfile" "$release"
install -d -m 0700 -o 10001 -g 10001 /volume1/docker/greybot/state
install -d -m 0700 -o 1000 -g 10 /volume1/docker/greybot/private
chmod 0700 /volume1/docker/greybot/state /volume1/docker/greybot/private
docker run --rm --network none --read-only --entrypoint python greybot-control:local \
  -c 'import greybot_control.web, greybot_control.worker; print("greyBot image imports passed")'
echo 'Image ready. Services remain stopped until private configuration and HTTPS are ready.'
