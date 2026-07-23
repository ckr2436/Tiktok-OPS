#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source_dir="${repo_root}/ops/hermes-video-analyst"
runtime_home="/home/hermes/.hermes-video-analyst"
credential_source_env="${CREDENTIAL_SOURCE_ENV:-/home/hermes/.hermes-ads-review/.env}"
gateway_env="${GATEWAY_ENV:-/etc/gmv/ai-gateway.env}"
unit_source="${repo_root}/ops/systemd/hermes-video-analyst.service"
unit_target="/etc/systemd/system/hermes-video-analyst.service"
stateless_patch="${repo_root}/ops/hermes-content-director/hermes-stateless-responses.patch"
max_tokens_patch="${repo_root}/ops/hermes-video-analyst/hermes-api-server-max-tokens.patch"
no_dump_patch="${repo_root}/ops/hermes-video-analyst/hermes-stateless-no-request-dump.patch"

test -f "${source_dir}/config.yaml"
test -f "${source_dir}/SOUL.md"
test -f "${unit_source}"
test -f "${credential_source_env}"
test -f "${gateway_env}"
git -C /home/hermes/hermes-agent apply --check --reverse "${stateless_patch}"
git -C /home/hermes/hermes-agent apply --check --reverse "${max_tokens_patch}"
git -C /home/hermes/hermes-agent apply --check --reverse "${no_dump_patch}"

install -d -o hermes -g hermes -m 0700 "${runtime_home}"
install -o hermes -g hermes -m 0600 "${source_dir}/config.yaml" "${runtime_home}/config.yaml"
install -o hermes -g hermes -m 0600 "${source_dir}/SOUL.md" "${runtime_home}/SOUL.md"

credential_tmp="$(mktemp)"
trap 'rm -f "${credential_tmp}"' EXIT
umask 077
{
  printf 'API_SERVER_ENABLED=true\n'
  printf 'API_SERVER_HOST=127.0.0.1\n'
  printf 'API_SERVER_PORT=8647\n'
  printf 'API_SERVER_MODEL_NAME=gmv-ops-hermes-video-analyst\n'
  for key in API_SERVER_KEY; do
    value="$(awk -F= -v wanted="${key}" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "${credential_source_env}")"
    if [[ -z "${value}" ]]; then
      echo "missing required credential ${key} in ${credential_source_env}" >&2
      exit 1
    fi
    printf '%s=%s\n' "${key}" "${value}"
  done
  gateway_key="$(awk -F= '$1 == "GMV_AI_GATEWAY_KEY" {sub(/^[^=]*=/, ""); print; exit}' "${gateway_env}")"
  if [[ -z "${gateway_key}" ]]; then
    echo "missing required credential GMV_AI_GATEWAY_KEY in ${gateway_env}" >&2
    exit 1
  fi
  printf 'GMV_AI_GATEWAY_KEY=%s\n' "${gateway_key}"
} >"${credential_tmp}"
install -o hermes -g hermes -m 0600 "${credential_tmp}" "${runtime_home}/.env"

install -o root -g root -m 0644 "${unit_source}" "${unit_target}"
systemctl daemon-reload
systemctl enable hermes-video-analyst.service
systemctl restart hermes-video-analyst.service
