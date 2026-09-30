#!/usr/bin/env bash
# install-doctl.sh — put the official doctl on PATH inside a MARS sandbox (there is none by default).
# Pinned release + the sha256 values from that release's own checksums file (digitalocean/doctl v1.175.0,
# published 2026-09-25). Refuses on a checksum mismatch; never runs anything it downloaded before verifying it.
#   usage: bash mars/install-doctl.sh        → installs to $HOME/.local/bin/doctl, prints `doctl version`
# doctl reads DIGITALOCEAN_ACCESS_TOKEN from the environment itself — never pass or print the token.
set -euo pipefail
VER=1.175.0
case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64; SHA=c722a6d48fab51a5edf6926e1ddc07ed58481184cad19e187ed39a2c0dc297f4 ;;
  aarch64|arm64) ARCH=arm64; SHA=02ca6c2efc11c6a7fd27a2ff61b788fa82438cca208f9ab5991d2db03778be48 ;;
  *) echo "install-doctl: REFUSED — unsupported arch $(uname -m)" >&2; exit 2 ;;
esac
DEST="$HOME/.local/bin"
if [ -x "$DEST/doctl" ] && "$DEST/doctl" version 2>/dev/null | grep -q "doctl version $VER"; then
  echo "install-doctl: doctl $VER already installed at $DEST/doctl"; exit 0
fi
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
TGZ="doctl-$VER-linux-$ARCH.tar.gz"
curl -fsSL --retry 3 -o "$T/$TGZ" "https://github.com/digitalocean/doctl/releases/download/v$VER/$TGZ"
echo "$SHA  $T/$TGZ" | sha256sum -c - >/dev/null || { echo "install-doctl: REFUSED — checksum mismatch for $TGZ" >&2; exit 3; }
mkdir -p "$DEST" && tar -xzf "$T/$TGZ" -C "$T" && install -m 0755 "$T/doctl" "$DEST/doctl"
"$DEST/doctl" version | head -1
case ":$PATH:" in *":$DEST:"*) ;; *) echo "install-doctl: add to PATH: export PATH=\"$DEST:\$PATH\"" ;; esac
