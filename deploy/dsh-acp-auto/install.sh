#!/bin/sh
# Install the Auto review profile for dsh --profile acp-auto.
#
#   deploy/dsh-acp-auto/install.sh
#
# Copies the profile into $DSH_HOME/profiles/acp-auto (default: ~/.dsh/profiles/acp-auto).
# It does not touch the ACP profile or the gateway config; see docs/auto-review.md.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
DEST="$DSH_HOME/profiles/acp-auto"

mkdir -p "$DEST"
cp "$HERE/profile/package.json" "$HERE/profile/cordis.yml" "$HERE/profile/cordis.patch.yml" \
   "$HERE/profile/auto-select.mjs" "$DEST/"
printf 'installed %s\n' "$DEST"
printf 'Next: point the gateway dsh agent at --profile acp-auto and advertise an "auto" permissionMode.\n'
