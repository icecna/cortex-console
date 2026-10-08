#!/usr/bin/env bash
set -euo pipefail

CONF=/etc/ai-server.conf
launcher_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "${CONF}" ]]; then
    echo "ERROR: config not found: ${CONF}" >&2
    exit 1
fi

launcher=$(sed -n 's/^LAUNCHER[[:space:]]*=[[:space:]]*//p' "${CONF}" | head -1)

if [[ -z "${launcher}" ]]; then
    echo "ERROR: LAUNCHER not set in ${CONF}" >&2
    exit 1
fi

target="${launcher_dir}/${launcher}.sh"

if [[ ! -x "${target}" ]]; then
    echo "ERROR: launcher not found or not executable: ${target}" >&2
    exit 1
fi

echo "ai-server: launching ${launcher}" >&2
exec "${target}"