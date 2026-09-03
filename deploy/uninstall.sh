#!/usr/bin/env bash
# Remove wake's units and venv. Keeps the database and config unless --purge.
set -euo pipefail

PURGE=0
[[ ${1:-} == "--purge" ]] && PURGE=1
[[ ${EUID} -ne 0 ]] || { echo "Run this as your own user, not with sudo." >&2; exit 1; }

REPOSITORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/wake"
CONFIG_DIR="${HOME}/.config/wake"

UNITS=(wake-server.service wake-agent.service wake-sync.timer wake-sync.service)

for unit in "${UNITS[@]}"; do
  if [[ -f ${UNIT_DIR}/${unit} ]]; then
    systemctl --user disable --now "${unit}" 2>/dev/null || true
    rm -f "${UNIT_DIR}/${unit}"
    echo "Removed ${unit}."
  fi
done
systemctl --user daemon-reload

# Disabling and deleting is not enough. A unit that was in a failed state keeps
# a runtime entry after its file is gone -- `systemctl --user list-units --all`
# then shows it as "not-found failed", and systemd holds a job for a unit it
# can no longer find. Verified on this box: only reset-failed clears it, and a
# daemon-reload does not. Harmless on units that never failed.
for unit in "${UNITS[@]}"; do
  systemctl --user reset-failed "${unit}" 2>/dev/null || true
done

# Nor does removing a timer remove its stamp file. Current units set no
# Persistent=, so they write none -- but a wake installed before that change
# armed wake-sync.timer with it and left one behind, and systemd keeps that
# file after the unit is disabled, deleted and reset. Verified on this box.
STAMP_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/systemd/timers"
for unit in "${UNITS[@]}"; do
  rm -f "${STAMP_DIR}/stamp-${unit}"
done

# Refuse to rm anything that resolved to a surprise. The variables are built
# from $HOME, and an empty $HOME would otherwise aim this at /.
for path in "${STATE_DIR}" "${CONFIG_DIR}"; do
  case "${path}" in
    ""|"/"|"/home"|"${HOME}"|"${HOME}/") echo "Refusing to remove '${path}'." >&2; exit 1 ;;
  esac
done

if [[ -d ${REPOSITORY}/.venv ]]; then
  rm -rf "${REPOSITORY}/.venv"
  echo "Removed ${REPOSITORY}/.venv."
fi

if [[ ${PURGE} -eq 1 ]]; then
  rm -rf "${STATE_DIR}" "${CONFIG_DIR}"
  echo "Purged ${STATE_DIR} and ${CONFIG_DIR}."
else
  echo "Kept ${STATE_DIR} and ${CONFIG_DIR}; pass --purge to remove them too."
fi
