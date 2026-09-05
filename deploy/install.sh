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
#
# Interactive by default, because `ownbox install wake` runs this with ownbox's
# own stdio (ownbox/store.py runs setup commands through subprocess.run with
# neither capture_output nor a stdin redirect, so the operator's terminal is
# inherited). Every prompt is guarded on `[ -t 0 ]`: with no TTY it takes the
# documented default, says which and why, and continues. It must never block --
# ownbox's command timeout is 1800s, so a prompt nobody can answer would hang
# for half an hour before failing.
set -euo pipefail

# Flags win over environment, environment wins over prompting, prompting wins
# over the defaults. Anything supplied by flag or environment skips its prompt,
# which is what makes the whole thing scriptable.
ROLE="${WAKE_INSTALL_ROLE:-}"
UNIT="${WAKE_INSTALL_UNIT:-}"
SERVER_URL="${WAKE_INSTALL_SERVER_URL:-}"
API_KEY="${WAKE_INSTALL_API_KEY:-}"
MAC="${WAKE_INSTALL_MAC:-}"
WOL_BROADCAST="${WAKE_INSTALL_WOL_BROADCAST:-}"
HOTLINE_URL="${WAKE_INSTALL_HOTLINE_URL:-}"
HOTLINE_KEY="${WAKE_INSTALL_HOTLINE_KEY:-}"
NO_PROMPT="${WAKE_INSTALL_NO_PROMPT:-0}"
TAKEOVER="${WAKE_INSTALL_TAKEOVER:-0}"
ROLE_GIVEN=0
[[ -n ${ROLE} ]] && ROLE_GIVEN=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="${2:-}"; ROLE_GIVEN=1; shift 2 ;;
    --unit) UNIT="${2:-}"; shift 2 ;;
    --server-url) SERVER_URL="${2:-}"; shift 2 ;;
    --api-key) API_KEY="${2:-}"; shift 2 ;;
    --mac) MAC="${2:-}"; shift 2 ;;
    --wol-broadcast) WOL_BROADCAST="${2:-}"; shift 2 ;;
    --hotline-url) HOTLINE_URL="${2:-}"; shift 2 ;;
    --hotline-key) HOTLINE_KEY="${2:-}"; shift 2 ;;
    --no-prompt) NO_PROMPT=1; shift ;;
    --takeover) TAKEOVER=1; shift ;;
    -h|--help)
      cat <<'USAGE'
usage: deploy/install.sh [--role server|device] [--unit server|agent|timer|none]
                         [--server-url URL] [--api-key KEY] [--mac ADDRESS]
                         [--wol-broadcast ADDRESS] [--hotline-url URL]
                         [--hotline-key KEY] [--no-prompt] [--takeover]

Prompts for anything not supplied, when there is a terminal to prompt on.
Environment equivalents, each skipping its prompt:

  WAKE_INSTALL_ROLE           server | device         (default: device)
  WAKE_INSTALL_UNIT           server|agent|timer|none (default: follows the role)
  WAKE_INSTALL_SERVER_URL     the server's base URL, devices only
  WAKE_INSTALL_API_KEY        the shared secret, both roles
  WAKE_INSTALL_MAC            this machine's MAC, what wol tasks target by
                              default; "none" to store none  (default: detected)
  WAKE_INSTALL_WOL_BROADCAST  broadcast address for wol      (default: blank)
  WAKE_INSTALL_HOTLINE_URL    hotline-ios base URL, for the notify/call backends
  WAKE_INSTALL_HOTLINE_KEY    hotline-ios shared key
  WAKE_INSTALL_NO_PROMPT      1 to never prompt even on a terminal
  WAKE_INSTALL_TAKEOVER       1 to repoint a unit that belongs to another checkout
USAGE
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ ${EUID} -ne 0 ]] || { echo "Run this as your own user, not with sudo." >&2; exit 1; }

REPOSITORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f ${REPOSITORY}/pyproject.toml ]] || {
  echo "Run this from the wake repository." >&2; exit 1; }

VENV="${REPOSITORY}/.venv"
CONFIG_DIR="${HOME}/.config/wake"
CONFIG_FILE="${CONFIG_DIR}/wake.env"
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/wake"
UNIT_DIR="${HOME}/.config/systemd/user"

interactive() { [[ ${NO_PROMPT} -ne 1 && -t 0 ]]; }

# `read` returns non-zero at EOF, which under `set -e` would abort the install
# rather than fall through to the default. Hence the `|| true` on every read.
ask() {  # ask <prompt> <default>; echoes the answer
  local reply=""
  read -r -p "$1" reply || true
  printf '%s' "${reply:-$2}"
}

# ---- role ----------------------------------------------------------------
# An existing config already answers this, and re-running install must not
# quietly change the role of a machine that is already deployed.
EXISTING_ROLE=""
if [[ -f ${CONFIG_FILE} ]]; then
  EXISTING_ROLE="$(sed -n 's/^[[:space:]]*ROLE[[:space:]]*=[[:space:]]*//p' "${CONFIG_FILE}" \
    | tail -n1 | tr -d '"'"'"' \t\r')"
fi

if [[ -z ${ROLE} && -n ${EXISTING_ROLE} ]]; then
  ROLE="${EXISTING_ROLE}"
  echo "Found ${CONFIG_FILE} with ROLE=${ROLE}; keeping it and not asking."
elif [[ -z ${ROLE} ]]; then
  if interactive; then
    while :; do
      answer="$(ask 'Is this machine the wake server or a device? [device/server] ' device)"
      case "${answer,,}" in
        d|device) ROLE="device"; break ;;
        s|server) ROLE="server"; break ;;
        *) echo "Answer 'device' or 'server'." ;;
      esac
    done
  else
    ROLE="device"
    echo "No terminal to ask on: installing as a device (the default)."
    echo "Pass --role server or set WAKE_INSTALL_ROLE to choose."
  fi
fi

[[ ${ROLE} == "server" || ${ROLE} == "device" ]] || {
  echo "--role must be server or device, not '${ROLE}'." >&2; exit 1; }

if [[ ${ROLE_GIVEN} -eq 1 && -n ${EXISTING_ROLE} && ${EXISTING_ROLE} != "${ROLE}" ]]; then
  echo "Note: ${CONFIG_FILE} says ROLE=${EXISTING_ROLE}, you asked for ${ROLE}."
  echo "      The unit will follow ${ROLE}; the config file is left alone."
  echo "      Edit ROLE in that file yourself if you meant to switch this machine over."
fi

# ---- the server's address, for a device ----------------------------------
normalise_url() {  # normalise_url <url> [default port]; bare host -> http://host:8788
  local url="${1//[[:space:]]/}" default_port="${2:-8788}"
  [[ -n ${url} ]] || return 0
  [[ ${url} == *"://"* ]] || url="http://${url}"
  local scheme="${url%%://*}" rest="${url#*://}"
  local authority="${rest%%/*}" path=""
  [[ ${rest} == */* ]] && path="/${rest#*/}"
  # Only supply the default port when the authority has none. A bracketed host
  # is a literal IPv6 address and its colons are not a port separator; a bare
  # unbracketed IPv6 address is ambiguous with one and is not handled here --
  # write it as [::1]:8788.
  if [[ ${authority} == "["*"]" ]] || [[ ${authority} != *:* ]]; then
    authority="${authority}:${default_port}"
  fi
  url="${scheme}://${authority}${path}"
  printf '%s' "${url%/}"
}

if [[ ${ROLE} == "device" && -z ${SERVER_URL} && -z ${EXISTING_ROLE} ]]; then
  if interactive; then
    SERVER_URL="$(ask "Address of the wake server (host, host:port or full URL), blank to fill in later: " "")"
  else
    echo "No terminal to ask on: leaving SERVER_URL blank in the config."
    echo "Set WAKE_INSTALL_SERVER_URL or edit ${CONFIG_FILE} before starting."
  fi
fi
[[ -n ${SERVER_URL} ]] && SERVER_URL="$(normalise_url "${SERVER_URL}")"

# ---- the shared secret ---------------------------------------------------
# A device without it gets a 401 from any server that has one, so an install
# that skipped this would be "finished" and not actually able to sync. Blank is
# a valid answer -- a server with no key set does not check.
if [[ -z ${API_KEY} && -z ${EXISTING_ROLE} ]]; then
  if interactive; then
    if [[ ${ROLE} == "device" ]]; then
      API_KEY="$(ask "Shared key the server expects (X-Wake-Key), blank if it has none: " "")"
    else
      API_KEY="$(ask "Shared key devices must send (X-Wake-Key), blank to leave the port open: " "")"
    fi
  fi
fi

# ---- this machine's own hardware --------------------------------------------
# The gap this closes: the MAC is per-task `--target`, so before this a fresh
# install had nothing to wake and no obvious place to put an address. It is
# stored in the config instead, and `wake add --backend wol` with no --target
# fills it in at add time -- deliberately at add time, because the machine that
# ends up sending the packet is not the machine whose address it is and has no
# way to look the other one's config up.

detect_mac() {  # prints this machine's most likely MAC, or nothing
  local interface="" candidate address
  # The interface carrying the default route is the one a magic packet would
  # arrive on. `ip` is not guaranteed present, hence the sysfs fallback.
  if command -v ip >/dev/null 2>&1; then
    interface="$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}')"
  fi
  if [[ -z ${interface} ]]; then
    for candidate in /sys/class/net/*; do
      [[ -e ${candidate} ]] || continue
      [[ $(basename "${candidate}") != "lo" ]] || continue
      # type 1 is ARPHRD_ETHER. Everything else -- tunnels, tailscale, bridges
      # -- either has no MAC or has one no switch will ever see a packet for.
      [[ $(cat "${candidate}/type" 2>/dev/null || echo 0) == "1" ]] || continue
      [[ ! -e ${candidate}/device ]] && continue  # virtual: docker0, br-*, veth
      interface="$(basename "${candidate}")"
      break
    done
  fi
  [[ -n ${interface} ]] || return 0
  address="$(cat "/sys/class/net/${interface}/address" 2>/dev/null || true)"
  [[ ${address} != "00:00:00:00:00:00" ]] || return 0
  printf '%s' "${address}"
}

detected_interface_is_wireless() {
  local interface=""
  command -v ip >/dev/null 2>&1 || return 1
  interface="$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}')"
  [[ -n ${interface} && -e /sys/class/net/${interface}/wireless ]]
}

valid_mac() {  # valid_mac <address>
  local cleaned="${1//[^0-9a-fA-F]/}"
  [[ ${#cleaned} -eq 12 ]]
}

if [[ -z ${MAC} && -z ${EXISTING_ROLE} ]]; then
  DETECTED_MAC="$(detect_mac)"
  if interactive; then
    while :; do
      MAC="$(ask "This machine's MAC address, so something can wake it \
[${DETECTED_MAC:-none}]: " "${DETECTED_MAC}")"
      [[ -z ${MAC} || ${MAC,,} == "none" ]] && { MAC=""; break; }
      valid_mac "${MAC}" && break
      echo "  '${MAC}' is not a MAC address -- twelve hex digits, any separator."
    done
  else
    MAC="${DETECTED_MAC}"
    if [[ -n ${MAC} ]]; then
      echo "No terminal to ask on: using the detected MAC ${MAC}."
      echo "Pass --mac (or --mac none) to choose."
    fi
  fi
elif [[ ${MAC,,} == "none" ]]; then
  MAC=""
fi

if [[ -n ${MAC} ]] && ! valid_mac "${MAC}"; then
  echo "Ignoring --mac '${MAC}': not twelve hex digits." >&2
  MAC=""
fi

if [[ -n ${MAC} ]] && detected_interface_is_wireless; then
  echo "Note: the default route is on a wireless interface. Wake-on-LAN over"
  echo "  WiFi needs WoWLAN support on both the card and the access point, and"
  echo "  usually does not survive a suspend. A wired MAC is the reliable one."
fi

# Only worth asking once someone has a MAC to send packets to. The default,
# 255.255.255.255, reaches the local subnet and is right for most installs;
# this is for the case where the sender is somewhere else.
if [[ -n ${MAC} && -z ${WOL_BROADCAST} && -z ${EXISTING_ROLE} ]]; then
  if interactive; then
    WOL_BROADCAST="$(ask "Broadcast address for wake-on-LAN packets, blank for \
the local subnet: " "")"
  fi
fi

# ---- hotline-ios, for the notify and call backends -------------------------
# Both backends are inert without a URL, and there is no default worth
# guessing: it is another machine on the operator's own network.
if [[ -z ${HOTLINE_URL} && -z ${EXISTING_ROLE} ]]; then
  if interactive; then
    HOTLINE_URL="$(ask "hotline-ios address for the notify/call backends, \
blank to skip: " "")"
  else
    echo "No terminal to ask on: leaving HOTLINE_IOS_URL blank."
    echo "The notify and call backends stay unavailable until it is set."
  fi
fi
if [[ -n ${HOTLINE_URL} ]]; then
  HOTLINE_URL="$(normalise_url "${HOTLINE_URL}" 8789)"
  if [[ -z ${HOTLINE_KEY} && -z ${EXISTING_ROLE} ]] && interactive; then
    HOTLINE_KEY="$(ask "hotline-ios shared key (X-Hotline-Key), blank if it \
has none: " "")"
  fi
fi

# ---- venv ----------------------------------------------------------------
if [[ ! -x ${VENV}/bin/python ]]; then
  python3 -m venv "${VENV}"
  echo "Created ${VENV}."
fi
"${VENV}/bin/python" -m pip install --quiet --upgrade pip
"${VENV}/bin/python" -m pip install --quiet "${REPOSITORY}"
echo "Installed wake into ${VENV}."

# ---- does that server answer, and is it wake? ----------------------------
# Reported, never enforced. A device is legitimately installed before its
# server exists -- and `wake agent` treats an unreachable server as retryable
# rather than fatal, so refusing the install here would be stricter than the
# thing being installed.
#
# The check is not "did something return 200". wake's default port is 8788 and
# other things answer on it: on the machine this was written for, hotlined
# holds 8788 and serves a /health that is valid JSON with an "ok" key, which an
# HTTP-status check accepted as a wake server. So the payload has to look like
# wake's health() -- ok/revision/role -- and the role has to be "server".
if [[ -n ${SERVER_URL} ]]; then
  if "${VENV}/bin/python" - "${SERVER_URL}" <<'PROBE'
import json, sys, urllib.error, urllib.request

url = sys.argv[1].rstrip("/") + "/health"
try:
    with urllib.request.urlopen(url, timeout=5) as response:
        body = json.loads(response.read() or b"{}")
except urllib.error.HTTPError as exc:
    print(f"  {url} answered HTTP {exc.code}.")
    raise SystemExit(1)
except Exception as exc:
    print(f"  could not reach {url}: {exc}")
    raise SystemExit(1)

if not isinstance(body, dict) or "role" not in body or "revision" not in body:
    print(f"  something answers {url}, but it is not wake: {json.dumps(body)[:120]}")
    raise SystemExit(1)
if body.get("role") != "server":
    print(f"  {url} is a wake {body.get('role')!r}, not the server.")
    raise SystemExit(1)
print(f"  {url}: wake server, revision {body.get('revision')}.")
PROBE
  then
    echo "Server ${SERVER_URL} looks right."
  else
    echo "Installing anyway -- a device is allowed to exist before its server"
    echo "  does, and the agent retries. Fix SERVER_URL in the config if that"
    echo "  address was wrong."
  fi
fi

# ---- directories ---------------------------------------------------------
# Every directory any unit names in StateDirectory= or ReadWritePaths= is
# created here. A hardened unit pointed at a path that does not exist fails at
# namespace setup, before ExecStart, with a 226/NAMESPACE that says nothing
# about the real cause -- profiler has shipped exactly that bug for a year.
install -d -m 0700 "${STATE_DIR}"
install -d -m 0700 "${CONFIG_DIR}"

# ---- config --------------------------------------------------------------
if [[ ! -f ${CONFIG_FILE} ]]; then
  install -m 0600 "${REPOSITORY}/deploy/wake.env.example" "${CONFIG_FILE}"
  {
    printf '\n# Written by deploy/install.sh.\n'
    printf 'ROLE=%s\n' "${ROLE}"
    if [[ -n ${SERVER_URL} ]]; then printf 'SERVER_URL=%s\n' "${SERVER_URL}"; fi
    if [[ -n ${API_KEY} ]]; then printf 'API_KEY=%s\n' "${API_KEY}"; fi
    if [[ -n ${MAC} ]]; then printf 'MAC=%s\n' "${MAC}"; fi
    if [[ -n ${WOL_BROADCAST} ]]; then printf 'WOL_BROADCAST=%s\n' "${WOL_BROADCAST}"; fi
    if [[ -n ${HOTLINE_URL} ]]; then printf 'HOTLINE_IOS_URL=%s\n' "${HOTLINE_URL}"; fi
    if [[ -n ${HOTLINE_KEY} ]]; then printf 'HOTLINE_IOS_KEY=%s\n' "${HOTLINE_KEY}"; fi
  } >> "${CONFIG_FILE}"
  echo "Wrote ${CONFIG_FILE} (ROLE=${ROLE})."
  [[ ${ROLE} == "device" && -z ${SERVER_URL} ]] && \
    echo "  SERVER_URL is still blank -- set it there before this device can sync."
else
  echo "Keeping the existing ${CONFIG_FILE}; nothing in it was changed."
fi

# ---- unit ----------------------------------------------------------------
# `--unit none` installs nothing, for a machine where wake is only ever run by
# hand or from a bot.
if [[ -z ${UNIT} ]]; then
  if [[ ${ROLE} == "server" ]]; then UNIT="server"; else UNIT="agent"; fi
fi

# The unit files carry @WAKE_ROOT@ rather than a literal path. They used to
# hardcode %h/data/wake, which is only correct for one checkout on one machine:
# installed from anywhere else -- ownbox checks out to
# ~/.local/share/ownbox/tools/wake -- systemd would start a binary that is not
# there and report 203/EXEC, which names no cause at all.
render_unit() {  # render_unit <source> <destination>
  sed "s|@WAKE_ROOT@|${REPOSITORY}|g" "$1" > "$2.tmp"
  install -Dm644 "$2.tmp" "$2"
  rm -f "$2.tmp"
}

# Two checkouts of wake on one machine share a single systemd user namespace,
# so an install can silently repoint -- and restart -- a daemon that belongs to
# the other one. That is how you move a live agent, and its scheduled power
# tasks, onto a checkout nobody meant to deploy. Installing over another
# checkout's unit therefore has to be asked for.
# systemd expands %h itself, so a unit written by an older install.sh says
# "ExecStart=%h/data/wake/..." and a literal string compare calls it foreign.
# Expand it the same way the user manager does before deciding.
unit_exec_root() {  # unit_exec_root <unit file>; prints the ExecStart binary path
  [[ -f $1 ]] || return 1
  local line
  line="$(sed -n 's/^ExecStart=//p' "$1" | head -n1)"
  [[ -n ${line} ]] || return 1
  printf '%s' "${line//%h/${HOME}}"
}

foreign_unit() {  # foreign_unit <unit filename>
  local exec_path
  exec_path="$(unit_exec_root "${UNIT_DIR}/$1")" || return 1
  [[ ${exec_path} != "${REPOSITORY}/"* ]]
}

case "${UNIT}" in
  none|server|agent|timer) ;;
  *) echo "unknown --unit '${UNIT}'" >&2; exit 2 ;;
esac

if [[ ${UNIT} != "none" && ${TAKEOVER} -ne 1 ]]; then
  for candidate in "wake-${UNIT}.service" wake-sync.service; do
    if foreign_unit "${candidate}"; then
      echo "${UNIT_DIR}/${candidate} already exists and runs a different checkout:"
      echo "  $(unit_exec_root "${UNIT_DIR}/${candidate}")"
      echo "Not touching it. This install stops at the venv and the config."
      echo "  --takeover (or WAKE_INSTALL_TAKEOVER=1) repoints it at ${REPOSITORY}."
      echo "  --unit none installs no unit at all and silences this."
      UNIT="none"
      break
    fi
  done
fi

INSTALLED_UNITS=()
case "${UNIT}" in
  none) echo "No systemd unit installed." ;;
  server|agent)
    install -d -m 0755 "${UNIT_DIR}"
    render_unit "${REPOSITORY}/systemd/wake-${UNIT}.service" \
      "${UNIT_DIR}/wake-${UNIT}.service"
    INSTALLED_UNITS=("wake-${UNIT}.service")
    ;;
  timer)
    install -d -m 0755 "${UNIT_DIR}"
    render_unit "${REPOSITORY}/systemd/wake-sync.service" "${UNIT_DIR}/wake-sync.service"
    render_unit "${REPOSITORY}/systemd/wake-sync.timer" "${UNIT_DIR}/wake-sync.timer"
    INSTALLED_UNITS=("wake-sync.timer")
    ;;
esac

if [[ ${#INSTALLED_UNITS[@]} -gt 0 ]]; then
  systemctl --user daemon-reload
  for unit in "${INSTALLED_UNITS[@]}"; do
    # The unit file was written under $HOME, but `systemctl --user` talks to a
    # manager that decided its own home at login and ignores ours. Where the
    # two disagree -- a faked $HOME, `sudo -u`, a container sharing the host
    # bus -- enable and restart would act on a completely different machine's
    # installation. Found the hard way: a sandboxed test run with $HOME
    # redirected wrote its unit into the sandbox and restarted the real
    # daemon. Check what systemd actually resolved before touching it.
    fragment="$(systemctl --user show "${unit}" -p FragmentPath --value 2>/dev/null || true)"
    if [[ ${fragment} != "${UNIT_DIR}/${unit}" ]]; then
      echo "systemd resolves ${unit} to '${fragment:-nothing}', not the file just" >&2
      echo "  written at ${UNIT_DIR}/${unit}. Refusing to enable or start it," >&2
      echo "  because that would act on a different installation than this one." >&2
      echo "  \$HOME is '${HOME}'; the user manager may not agree." >&2
      echo "  Re-run with --unit none if you only want the venv and the config." >&2
      exit 1
    fi
    systemctl --user enable "${unit}" >/dev/null
    echo "Enabled ${unit} (ExecStart under ${REPOSITORY})."
    # Starting is the difference between "installed" and "running", so it is
    # attempted -- but a device whose server is not up yet, or whose config is
    # still a stub, is a normal state and must not fail the install.
    if systemctl --user restart "${unit}"; then
      echo "Started ${unit}."
    else
      echo "Could not start ${unit}. The install is otherwise complete."
      echo "  systemctl --user status ${unit}"
      echo "  journalctl --user -u ${unit} -n 30"
    fi
  done
fi

echo
echo "wake is installed from ${REPOSITORY} as a ${ROLE}."
echo "  config: ${CONFIG_FILE}"
if [[ -n ${MAC} ]]; then echo "  mac:    ${MAC} (wol tasks target this by default)"; fi
echo "  state:  ${STATE_DIR}"
echo "  cli:    ${VENV}/bin/wake"
