#!/usr/bin/env bash
# Install wake for the current user: a venv, a config file, and one systemd
# user unit chosen by role.
#
# Deliberately a *user* install, not the root install profiler uses. wake needs
# no privileges beyond this user's, and the task database belongs next to the
# operator's other state rather than in /var/lib.
#
# Re-running is safe: every step is guarded on what already exists, and an
# existing config file is never overwritten.
set -euo pipefail

ROLE="device"
UNIT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="${2:-}"; shift 2 ;;
    --unit) UNIT="${2:-}"; shift 2 ;;
    -h|--help)
      echo "usage: deploy/install.sh [--role server|device] [--unit server|agent|timer|none]"
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ ${EUID} -ne 0 ]] || { echo "Run this as your own user, not with sudo." >&2; exit 1; }
[[ ${ROLE} == "server" || ${ROLE} == "device" ]] || {
  echo "--role must be server or device, not '${ROLE}'." >&2; exit 1; }

REPOSITORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f ${REPOSITORY}/pyproject.toml ]] || {
  echo "Run this from the wake repository." >&2; exit 1; }

VENV="${REPOSITORY}/.venv"
CONFIG_DIR="${HOME}/.config/wake"
CONFIG_FILE="${CONFIG_DIR}/wake.env"
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/wake"
UNIT_DIR="${HOME}/.config/systemd/user"

# Default the unit to the role. `--unit none` installs nothing, for a machine
# where wake is only ever run by hand or from a bot.
if [[ -z ${UNIT} ]]; then
  if [[ ${ROLE} == "server" ]]; then UNIT="server"; else UNIT="agent"; fi
fi

if [[ ! -x ${VENV}/bin/python ]]; then
  python3 -m venv "${VENV}"
  echo "Created ${VENV}."
fi
"${VENV}/bin/python" -m pip install --quiet --upgrade pip
"${VENV}/bin/python" -m pip install --quiet "${REPOSITORY}"
echo "Installed wake into ${VENV}."

# Every directory any unit names in StateDirectory= or ReadWritePaths= is
# created here. A hardened unit pointed at a path that does not exist fails at
# namespace setup, before ExecStart, with a 226/NAMESPACE that says nothing
# about the real cause -- profiler has shipped exactly that bug for a year.
install -d -m 0700 "${STATE_DIR}"
install -d -m 0700 "${CONFIG_DIR}"

if [[ ! -f ${CONFIG_FILE} ]]; then
  install -m 0600 "${REPOSITORY}/deploy/wake.env.example" "${CONFIG_FILE}"
  printf '\nROLE=%s\n' "${ROLE}" >> "${CONFIG_FILE}"
  echo "Created ${CONFIG_FILE}; set SERVER_URL and API_KEY before starting."
else
  echo "Keeping the existing ${CONFIG_FILE}."
fi

case "${UNIT}" in
  none) echo "No systemd unit installed, as asked." ;;
  server|agent)
    install -Dm644 "${REPOSITORY}/systemd/wake-${UNIT}.service" \
      "${UNIT_DIR}/wake-${UNIT}.service"
    systemctl --user daemon-reload
    systemctl --user enable "wake-${UNIT}.service"
    echo "Enabled wake-${UNIT}.service (not started; start it once the config is filled in)."
    ;;
  timer)
    install -Dm644 "${REPOSITORY}/systemd/wake-sync.service" "${UNIT_DIR}/wake-sync.service"
    install -Dm644 "${REPOSITORY}/systemd/wake-sync.timer" "${UNIT_DIR}/wake-sync.timer"
    systemctl --user daemon-reload
    systemctl --user enable wake-sync.timer
    echo "Enabled wake-sync.timer (not started; start it once the config is filled in)."
    ;;
  *) echo "unknown --unit '${UNIT}'" >&2; exit 2 ;;
esac

if [[ ${UNIT} == "none" ]]; then
  echo "Installed. Edit ${CONFIG_FILE}, then run wake by hand or from a bot."
elif [[ ${UNIT} == "timer" ]]; then
  echo "Installed. Edit ${CONFIG_FILE}, then: systemctl --user start wake-sync.timer"
else
  echo "Installed. Edit ${CONFIG_FILE}, then: systemctl --user start wake-${UNIT}.service"
fi
