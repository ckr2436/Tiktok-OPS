#!/usr/bin/env bash
set -euo pipefail

# Build the downloadable Agent as a GUI subsystem executable.  The agent has
# its own message boxes for user-facing install failures; it must never create
# a visible console window during install, startup, or self-update.
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
output=${1:-"$root/backend/assets/MYUPONA-HermesBridge.exe"}

cd "$root/hermes-bridge-agent"
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags='-H=windowsgui' -o "$output" .
