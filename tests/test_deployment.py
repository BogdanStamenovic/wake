"""What the deploy scripts and the Ownbox manifest promise each other.

These are cheap static checks for the things that only fail on a machine that
is not this one: a unit whose ExecStart points at somebody else's checkout, or
a manifest that quietly builds a second installation path beside deploy/.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parent.parent
SYSTEMD = REPOSITORY / "systemd"
INSTALL = REPOSITORY / "deploy" / "install.sh"
UNINSTALL = REPOSITORY / "deploy" / "uninstall.sh"

UNITS_WITH_EXEC = ["wake-agent.service", "wake-server.service", "wake-sync.service"]


@pytest.mark.parametrize("name", UNITS_WITH_EXEC)
def test_units_carry_no_hardcoded_checkout(name: str) -> None:
    """ExecStart must be a placeholder, not one machine's directory.

    The units used to say %h/data/wake, which is right for a single clone and
    wrong everywhere else -- Ownbox checks out to ~/.local/share/ownbox/tools,
    and systemd's only complaint about a missing binary is 203/EXEC.
    """
    text = (SYSTEMD / name).read_text()
    directives = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    exec_lines = [line for line in directives if line.startswith("ExecStart=")]
    assert exec_lines, f"{name} has no ExecStart"
    for line in exec_lines:
        assert line.startswith("ExecStart=@WAKE_ROOT@/"), line
    # Comments may still name the old path; they explain why it went away.
    assert not [line for line in directives if "%h/data/wake" in line]


def test_install_substitutes_the_placeholder() -> None:
    assert "@WAKE_ROOT@" in INSTALL.read_text()


def test_manifest_drives_the_deploy_scripts() -> None:
    manifest = yaml.safe_load((REPOSITORY / "ownbox.yaml").read_text())
    install = manifest["install"]
    assert install["setup"]["linux"] == ["bash deploy/install.sh"]
    assert install["update"]["linux"] == ["bash deploy/install.sh --no-prompt"]
    # A real uninstall, not the empty list this shipped with.
    assert install["remove"]["linux"] == ["bash deploy/uninstall.sh"]
    # The task database is the operator's data. profiler purges on remove
    # because its state is derived; wake's is not.
    assert not any("--purge" in command for command in install["remove"]["linux"])
    # Only Linux: every deploy path here is a systemd user unit.
    assert install["platforms"] == ["linux"]


def test_manifest_command_matches_the_venv_install_creates() -> None:
    manifest = yaml.safe_load((REPOSITORY / "ownbox.yaml").read_text())
    assert manifest["command"]["linux"] == ".venv/bin/wake"
    assert 'VENV="${REPOSITORY}/.venv"' in INSTALL.read_text()


@pytest.mark.parametrize("script", [INSTALL, UNINSTALL])
def test_scripts_parse(script: Path) -> None:
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("100.72.2.62", "http://100.72.2.62:8788"),
        ("100.72.2.62:9000", "http://100.72.2.62:9000"),
        ("http://box", "http://box:8788"),
        ("http://box/", "http://box:8788"),
        ("https://box:443/api", "https://box:443/api"),
        ("  box  ", "http://box:8788"),
        ("[::1]", "http://[::1]:8788"),
        ("[::1]:1234", "http://[::1]:1234"),
        ("", ""),
    ],
)
def test_server_url_normalisation(given: str, expected: str) -> None:
    """A person types a tailnet IP; the config needs a URL with a port."""
    script = (
        f"source <(sed -n '/^normalise_url/,/^}}/p' {INSTALL}); normalise_url '{given}'"
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout == expected


def test_uninstall_only_touches_its_own_units() -> None:
    """Two checkouts share one systemd user namespace; ownership is ExecStart."""
    text = UNINSTALL.read_text()
    assert 'grep -qF "ExecStart=${REPOSITORY}/"' in text
    assert "--all" in text


def test_install_refuses_to_hijack_another_checkouts_unit() -> None:
    text = INSTALL.read_text()
    assert "foreign_unit" in text
    assert "--takeover" in text


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize("script", [INSTALL, UNINSTALL])
def test_shellcheck(script: Path) -> None:
    subprocess.run(["shellcheck", str(script)], check=True)


def test_install_checks_systemd_resolves_the_file_it_wrote() -> None:
    """`systemctl --user` ignores our $HOME; enabling blind hits the real box."""
    text = INSTALL.read_text()
    assert "-p FragmentPath" in text
    assert 'if [[ ${fragment} != "${UNIT_DIR}/${unit}" ]]' in text
