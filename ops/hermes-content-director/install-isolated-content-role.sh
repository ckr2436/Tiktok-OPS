#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
role="${1:-}"
credential_source_env="${CREDENTIAL_SOURCE_ENV:-/home/hermes/.hermes-content-director/.env}"

case "${role}" in
  producer)
    role_dir="hermes-content-producer"
    runtime_home="/home/hermes/.hermes-content-producer"
    port="8648"
    model_name="gmv-ops-hermes-content-producer"
    route_role="director"
    ;;
  director)
    role_dir="hermes-content-director"
    runtime_home="/home/hermes/.hermes-content-director"
    port="8645"
    model_name="gmv-ops-hermes-content-director"
    route_role="director"
    ;;
  critic)
    role_dir="hermes-content-critic"
    runtime_home="/home/hermes/.hermes-content-critic"
    port="8646"
    model_name="gmv-ops-hermes-content-critic"
    route_role="critic"
    ;;
  *)
    echo "usage: $0 producer|director|critic" >&2
    exit 2
    ;;
esac

source_dir="${repo_root}/ops/${role_dir}"
unit_source="${repo_root}/ops/systemd/${role_dir}.service"
unit_target="/etc/systemd/system/${role_dir}.service"
stateless_patch="${repo_root}/ops/hermes-content-director/hermes-stateless-responses.patch"
routing_policy="${repo_root}/ops/hermes-content-director/routing-policy.json"
routing_sync="${repo_root}/ops/hermes-content-director/sync-role-model-routes.py"

test -f "${source_dir}/config.yaml"
test -f "${source_dir}/SOUL.md"
test -f "${unit_source}"
test -f "${credential_source_env}"
test -f /etc/gmv/ai-gateway.env
test -f "${routing_policy}"
test -f "${routing_sync}"
git -C /home/hermes/hermes-agent apply --check --reverse "${stateless_patch}"

# Materialize the role's configured logical model group before the gateway is
# restarted.  This is durable route configuration, not request-path fallback
# logic, and it inherits operator route enablement plus current circuit state.
(
  cd "${repo_root}/backend"
  /opt/gmv/python3.13/bin/python "${routing_sync}" \
    --role "${route_role}" \
    --policy "${routing_policy}"
  /opt/gmv/python3.13/bin/python "${routing_sync}" \
    --role visual_inspector \
    --policy "${routing_policy}"
)

install -d -o hermes -g hermes -m 0700 "${runtime_home}"
install -o hermes -g hermes -m 0600 \
  "${source_dir}/config.yaml" "${runtime_home}/config.yaml"
install -o hermes -g hermes -m 0600 \
  "${source_dir}/SOUL.md" "${runtime_home}/SOUL.md"

credential_tmp="$(mktemp)"
trap 'rm -f "${credential_tmp}"' EXIT
umask 077
{
  printf 'API_SERVER_ENABLED=true\n'
  printf 'API_SERVER_HOST=127.0.0.1\n'
  printf 'API_SERVER_PORT=%s\n' "${port}"
  printf 'API_SERVER_MODEL_NAME=%s\n' "${model_name}"
  for key in API_SERVER_KEY; do
    value="$(
      awk -F= -v wanted="${key}" '
        $1 == wanted {
          sub(/^[^=]*=/, "")
          print
          exit
        }
      ' "${credential_source_env}"
    )"
    if [[ -z "${value}" ]]; then
      echo "missing required credential ${key} in ${credential_source_env}" >&2
      exit 1
    fi
    printf '%s=%s\n' "${key}" "${value}"
  done
} >"${credential_tmp}"
install -o hermes -g hermes -m 0600 "${credential_tmp}" "${runtime_home}/.env"

install -o root -g root -m 0644 "${unit_source}" "${unit_target}"
systemctl daemon-reload
systemctl enable "${role_dir}.service"
# ``enable --now`` does not restart an already-running unit, so a config,
# executable patch, or EnvironmentFile update previously appeared deployed
# while the old process and credentials stayed resident.  Installation is a
# rolling replacement boundary for this isolated role.
systemctl restart "${role_dir}.service"
