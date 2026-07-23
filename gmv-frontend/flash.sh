#!/bin/bash
set -euo pipefail

DEPLOY_USER="gmv"

if [[ "$(id -un)" != "$DEPLOY_USER" ]]; then
    echo "ERROR: deployment must run as $DEPLOY_USER (current user: $(id -un))." >&2
    echo "Run: sudo -u $DEPLOY_USER -- /bin/bash ./flash.sh" >&2
    exit 1
fi

umask 022
cd "$(dirname "$0")"

PROJECT_ROOT="$(cd .. && pwd)"
TARGET_ROOT="$PROJECT_ROOT/frontend"
STAGING_ROOT="$PROJECT_ROOT/.frontend-staging-$$"
PREVIOUS_ROOT="$PROJECT_ROOT/.frontend-previous-$$"

cleanup() {
    rm -rf "$STAGING_ROOT"
    if [[ -d "$PREVIOUS_ROOT" && ! -e "$TARGET_ROOT" ]]; then
        mv "$PREVIOUS_ROOT" "$TARGET_ROOT"
    fi
    rm -rf "$PREVIOUS_ROOT"
}
trap cleanup EXIT

# The build output is offline.  The live Nginx root is not touched until the
# complete artifact and its permissions have both been validated.
rm -rf dist
pnpm run build
install -d -m 755 "$STAGING_ROOT"
cp -a dist/. "$STAGING_ROOT/"

find dist "$STAGING_ROOT" -type d -exec chmod 755 {} +
find dist "$STAGING_ROOT" -type f -exec chmod 644 {} +
if find dist "$STAGING_ROOT" ! -user "$DEPLOY_USER" -print -quit | grep -q .; then
    echo "ERROR: deployed files contain unexpected ownership." >&2
    exit 1
fi

if [[ -e "$TARGET_ROOT" ]]; then
    mv "$TARGET_ROOT" "$PREVIOUS_ROOT"
fi
if ! mv "$STAGING_ROOT" "$TARGET_ROOT"; then
    if [[ -d "$PREVIOUS_ROOT" ]]; then
        mv "$PREVIOUS_ROOT" "$TARGET_ROOT"
    fi
    echo "ERROR: frontend activation failed; previous deployment restored." >&2
    exit 1
fi
rm -rf "$PREVIOUS_ROOT"

if find "$TARGET_ROOT" ! -user "$DEPLOY_USER" -print -quit | grep -q .; then
    echo "ERROR: active frontend contains unexpected ownership." >&2
    exit 1
fi
echo "Deployment completed as $DEPLOY_USER; ownership and permissions verified."
