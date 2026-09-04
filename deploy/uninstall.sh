#!/usr/bin/env bash
# Remove what deploy/install.sh created for *this* checkout: its systemd units
# and its venv. Keeps the task database and the config unless --purge.
#
# Safe to run twice, and safe to run when nothing is installed.
set -euo pipefail

PURGE=0
ALL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    --all) ALL=1; shift ;;
    -h|--help)
      cat <<'USAGE'
usage: deploy/uninstall.sh [--purge] [--all]

  --purge  also delete the config and the task database (your data)
  --all    remove wake units even when they point at a different checkout

By default only units whose ExecStart lives under this repository are touched,
so uninstalling one checkout does not tear down another checkout's daemon.
USAGE
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ ${EUID} -ne 0 ]] || { echo "Run this as your own user, not with sudo." >&2; exit 1; }

REPOSITORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/wake"
CONFIG_DIR="${HOME}/.config/wake"

UNITS=(wake-server.service wake-agent.service wake-sync.timer wake-sync.service)

# A wake unit installed from a *different* checkout is somebody else's daemon.
# There can legitimately be two checkouts on one machine -- ownbox keeps its
# own under ~/.local/share/ownbox/tools/wake, separate from a working clone --
# and they share one systemd user namespace and one config file. Without this
# guard, uninstalling either one would stop and delete the other's running
# service, which is how you lose a scheduled poweroff by tidying up a checkout.
# Ownership is read off ExecStart, which install.sh renders with an absolute
# path into the repository it installed from.
owns_unit() {  # owns_unit <unit filename>
  local file="${UNIT_DIR}/$1"
  [[ -f ${file} ]] || return 1
  [[ ${ALL} -eq 1 ]] && return 0
  # The timer has no ExecStart of its own; it belongs to whoever owns the
  # service it triggers.
  [[ $1 == "wake-sync.timer" ]] && file="${UNIT_DIR}/wake-sync.service"
  [[ -f ${file} ]] || return 1
  grep -qF "ExecStart=${REPOSITORY}/" "${file}"
}

REMOVED=()
SKIPPED=()
for unit in "${UNITS[@]}"; do
  [[ -f ${UNIT_DIR}/${unit} ]] || continue
  if owns_unit "${unit}"; then
    systemctl --user disable --now "${unit}" 2>/dev/null || true
    rm -f "${UNIT_DIR}/${unit}"
    REMOVED+=("${unit}")
    echo "Removed ${unit}."
  else
    SKIPPED+=("${unit}")
  fi
done

if [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo "Left alone (installed from a different checkout): ${SKIPPED[*]}"
  echo "  Pass --all to remove them anyway."
fi

if [[ ${#REMOVED[@]} -gt 0 ]]; then
  systemctl --user daemon-reload

  # Disabling and deleting is not enough. A unit that was in a failed state keeps
  # a runtime entry after its file is gone -- `systemctl --user list-units --all`
  # then shows it as "not-found failed", and systemd holds a job for a unit it
  # can no longer find. Verified on this box: only reset-failed clears it, and a
  # daemon-reload does not. Harmless on units that never failed.
  for unit in "${REMOVED[@]}"; do
    systemctl --user reset-failed "${unit}" 2>/dev/null || true
  done

  # Nor does removing a timer remove its stamp file. Current units set no
  # Persistent=, so they write none -- but a wake installed before that change
  # armed wake-sync.timer with it and left one behind, and systemd keeps that
  # file after the unit is disabled, deleted and reset. Verified on this box.
  STAMP_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/systemd/timers"
  for unit in "${REMOVED[@]}"; do
    rm -f "${STAMP_DIR}/stamp-${unit}"
  done
fi

# Refuse to rm anything that resolved to a surprise. The variables are built
# from $HOME, and an empty $HOME would otherwise aim this at /.
for path in "${STATE_DIR}" "${CONFIG_DIR}" "${REPOSITORY}"; do
  case "${path}" in
    ""|"/"|"/home"|"${HOME}"|"${HOME}/") echo "Refusing to remove '${path}'." >&2; exit 1 ;;
  esac
done

if [[ -d ${REPOSITORY}/.venv ]]; then
  rm -rf "${REPOSITORY}/.venv"
  echo "Removed ${REPOSITORY}/.venv."
fi

echo
if [[ ${PURGE} -eq 1 ]]; then
  rm -rf "${STATE_DIR}" "${CONFIG_DIR}"
  echo "Purged ${STATE_DIR} and ${CONFIG_DIR}."
else
  echo "Left behind, because it is your data and not an install artifact:"
  [[ -e ${STATE_DIR} ]] && echo "  ${STATE_DIR}   -- the task database and its WAL"
  [[ -e ${CONFIG_DIR} ]] && echo "  ${CONFIG_DIR}  -- wake.env, including the shared key"
  echo "Run this again with --purge to delete those too."
fi
