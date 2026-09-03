#!/usr/bin/env bash
# Assemble the Lambda package that CDK ships, at build/lambda/.
#
# WHY THIS EXISTS. The CDK stack used to say Code.from_asset("../src"), which is the
# obvious thing and is wrong: src/ is the source, not the package. It has never been the
# package, because the function has one dependency that the runtime does not carry.
#
# PyNaCl is that dependency, and it is native, so the wheel has to match the Lambda's
# architecture rather than this laptop's. Downloading the linux/aarch64 wheel directly
# avoids needing Docker to cross-build. Signature verification is not somewhere to
# hand-roll crypto: libsodium is the audited implementation and this is the audited
# binding to it.
#
# The cost of getting this wrong is quiet. interactions.verify() fails CLOSED on
# ImportError -- it must, because Discord probes with invalid signatures and pulls the
# endpoint if one is ever waved through -- so a package with no PyNaCl does not crash,
# does not alarm, and does not appear in the health probes, which check the bot's Discord
# membership rather than its own endpoint. It just answers every slash command with a 401.
# That is what prod did from the CDK cutover on 2026-09-01 until this script existed; the
# only evidence was one interaction_rejected{reason:"bad_signature"} log line the
# afternoon somebody tried /progress.
#
# The Phase 1 parity gate did not catch it because it diffs IAM, schedules and function
# CONFIGURATION -- memory, timeout, architecture, env vars. Nothing in it ever looked at
# what was inside the zip.
#
# Idempotent: safe to re-run. Rebuilt from scratch every time, because a stale file left
# behind in a build directory is its own class of Friday evening.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/build/lambda"

rm -rf "$OUT"
mkdir -p "$OUT"

cp "$ROOT/src/"*.py "$OUT/"

# PyNaCl for signature verification, Pillow for drawing the first-kill card. Both are
# native, so both are pulled as linux/aarch64 wheels rather than whatever this laptop
# happens to be -- and both would fail at RUNTIME rather than at build time if the wrong
# architecture were shipped, which is the failure this whole script exists to prevent.
python3 -m pip install --quiet --platform manylinux2014_aarch64 --implementation cp \
  --python-version 3.12 --only-binary=:all: --target "$OUT" pynacl pillow
rm -rf "$OUT"/bin "$OUT"/*.dist-info

# The fonts the card is drawn with. A Lambda has no system fonts at all, and macOS's are
# Apple's to license rather than mine to redistribute, so DejaVu travels with the package
# under its own permissive licence.
mkdir -p "$OUT/fonts"
cp "$ROOT/assets/fonts/DejaVuSans.ttf" "$ROOT/assets/fonts/DejaVuSans-Bold.ttf" \
   "$ROOT/assets/fonts/LICENSE-DejaVu.txt" "$OUT/fonts/"

# __pycache__ would otherwise ride along from the pip install and, before this script, from
# src/ itself. It is dead weight in the zip and it changes the CDK asset hash on a laptop
# that has merely IMPORTED the module, which makes "did the code change" unanswerable from
# the diff CDK prints.
find "$OUT" -name __pycache__ -type d -prune -exec rm -rf {} +

python3 - "$OUT" <<'PY'
import pathlib, sys
out = pathlib.Path(sys.argv[1])
# The gate. A package that cannot verify a signature is a package that answers every slash
# command with a 401, so this refuses to hand CDK something to deploy rather than leaving
# it to be discovered in Discord.
assert (out / "nacl").is_dir(), "PyNaCl did not vendor into the package"
assert (out / "PIL").is_dir(), "Pillow did not vendor into the package"
assert (out / "fonts" / "DejaVuSans-Bold.ttf").is_file(), "the card fonts are missing"
assert (out / "handler.py").is_file(), "handler.py is missing from the package"
size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
print(f"    package: {size // 1024} KB  (stdlib + boto3 from the runtime, + PyNaCl, Pillow, fonts)")
PY

echo "==> build/lambda ready"
