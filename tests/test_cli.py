"""The CLI contract: what lands on stdout, what lands on stderr, what exits what.

Other tools parse this. `add` must print an id and nothing else; `list --json`
must print JSON and nothing else; every diagnostic goes to stderr.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wake.cli import main


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may read the operator's real ~/.config/wake/wake.env."""
    monkeypatch.setenv("WAKE_CONFIG", str(tmp_path / "absent.env"))
    monkeypatch.setenv("WAKE_DB_PATH", str(tmp_path / "wake.db"))
    monkeypatch.setenv("WAKE_ORIGIN", "testbox")
    monkeypatch.delenv("WAKE_SERVER_URL", raising=False)


def test_add_prints_only_the_id(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add", "--at", "+1h", "--task", "echo hi"]) == 0
    out = capsys.readouterr()
    assert out.out.strip().isalnum()
    assert len(out.out.strip()) == 32
    assert out.err == ""


def test_add_rejects_a_time_it_cannot_parse(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add", "--at", "whenever", "--task", "x"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "wake: error" in out.err


def test_add_rejects_a_relative_offset_with_no_unit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["add", "--at", "+5", "--task", "x"]) == 1
    assert "no unit" in capsys.readouterr().err


def test_a_missing_required_flag_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["add", "--at", "+1h"]) == 2
    assert "wake: error" in capsys.readouterr().err


def test_an_unknown_backend_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add", "--at", "+1h", "--task", "x", "--backend", "telepathy"]) == 2
    capsys.readouterr()


def test_list_json_is_parseable_and_alone_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["add", "--at", "+1h", "--task", "echo hi"])
    capsys.readouterr()
    assert main(["list", "--json"]) == 0
    out = capsys.readouterr()
    rows = json.loads(out.out)
    assert len(rows) == 1
    assert rows[0]["task"] == "echo hi"
    assert out.err == ""


def test_list_table_says_so_when_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    assert capsys.readouterr().out.strip() == "no tasks"


def test_list_hides_cancelled_tasks_until_all(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["add", "--at", "+1h", "--task", "echo hi"])
    task_id = capsys.readouterr().out.strip()
    main(["cancel", task_id])
    capsys.readouterr()

    main(["list", "--json"])
    assert json.loads(capsys.readouterr().out) == []
    main(["list", "--all", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["status"] == "cancelled"


def test_cancel_reports_a_missing_id_as_a_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["cancel", "nosuchid"]) == 1
    assert "no such task" in capsys.readouterr().err


def test_the_on_flag_records_an_owner(capsys: pytest.CaptureFixture[str]) -> None:
    main(["add", "--at", "+1h", "--task", "x", "--on", "laptop"])
    capsys.readouterr()
    main(["list", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["owner"] == "laptop"


def test_an_explicit_id_is_honoured(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add", "--at", "+1h", "--task", "x", "--id", "my-own-id"]) == 0
    assert capsys.readouterr().out.strip() == "my-own-id"


def test_quiet_silences_progress_but_not_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["-q", "cancel", "nosuchid"]) == 1
    assert "wake: error" in capsys.readouterr().err


def test_fire_runs_a_task_ahead_of_its_schedule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "fired"
    main(["add", "--at", "+10h", "--task", f"touch {marker}"])
    task_id = capsys.readouterr().out.strip()

    assert main(["fire", task_id]) == 0
    capsys.readouterr()
    assert marker.exists()
    main(["list", "--all", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["status"] == "fired"


def test_a_failing_fire_records_the_error_and_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["add", "--at", "+10h", "--task", "exit 7"])
    task_id = capsys.readouterr().out.strip()

    assert main(["fire", task_id]) == 1
    capsys.readouterr()
    main(["list", "--all", "--json"])
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "failed"
    assert "exited 7" in row["error"]


def test_sync_on_a_server_is_a_no_op(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAKE_ROLE", "server")
    assert main(["sync"]) == 0
    assert "source of truth" in capsys.readouterr().err


def test_sync_without_a_server_url_fails_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["sync"]) == 1
    assert "no server configured" in capsys.readouterr().err


def test_agent_once_fires_only_this_machines_tasks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"
    main(["add", "--at", "+0s", "--task", f"touch {mine}", "--on", "testbox"])
    main(["add", "--at", "+0s", "--task", f"touch {theirs}", "--on", "otherbox"])
    capsys.readouterr()

    assert main(["agent", "--once"]) == 0
    capsys.readouterr()
    assert mine.exists()
    assert not theirs.exists(), "a device must not run another machine's task"


def test_agent_once_leaves_server_owned_tasks_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "servers"
    main(["add", "--at", "+0s", "--task", f"touch {marker}"])
    capsys.readouterr()
    assert main(["agent", "--once"]) == 0
    capsys.readouterr()
    assert not marker.exists(), "an unassigned task belongs to the server"


def test_a_bad_config_file_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.env"
    bad.write_text("this line has no equals sign\n")
    monkeypatch.setenv("WAKE_CONFIG", str(bad))
    assert main(["list"]) == 2
    assert "expected KEY=value" in capsys.readouterr().err


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert "wake" in capsys.readouterr().out


def test_re_adding_the_same_id_rearms_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """track re-arms track-<assignment> on every run; this must never crash."""
    assert main(["add", "--at", "+1h", "--task", "track run abc", "--id", "track-abc"]) == 0
    assert capsys.readouterr().out.strip() == "track-abc"

    assert main(["add", "--at", "+2h", "--task", "track run abc", "--id", "track-abc"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "track-abc"
    assert out.err == "", "a re-arm is not a warning"

    main(["list", "--all", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1, "never two rows racing to fire"


def test_rearming_a_fired_task_puts_it_back_in_the_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran"
    main(["add", "--at", "+1h", "--task", f"touch {marker}", "--id", "track-abc"])
    main(["fire", "track-abc"])
    capsys.readouterr()

    main(["add", "--at", "+2h", "--task", f"touch {marker}", "--id", "track-abc"])
    capsys.readouterr()
    main(["list", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert [r["status"] for r in rows] == ["pending"]


def test_firing_a_self_rearming_task_leaves_it_scheduled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wake fire` on a recurring task must not kill it. Exercised through a
    real child process, which is what actually re-arms in production."""
    import sys
    import textwrap

    db_path = str(tmp_path / "wake.db")
    script = tmp_path / "rearm.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from wake.db import WakeDB
            with WakeDB({db_path!r}) as handle:
                handle.add(
                    task="next", at=9999999999.0, backend="shell",
                    target=None, origin="testbox", id="recurring",
                )
            """
        )
    )
    main([
        "add", "--at", "+1h", "--id", "recurring",
        "--task", f"{sys.executable} {script}",
    ])
    capsys.readouterr()

    assert main(["fire", "recurring"]) == 0
    assert "re-armed itself" in capsys.readouterr().err

    main(["list", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert [r["status"] for r in rows] == ["pending"], "the timer must survive"


# -- --every ----------------------------------------------------------------


def test_add_stores_a_period(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add", "--at", "+1h", "--every", "daily", "--task", "x"]) == 0
    capsys.readouterr()
    assert main(["list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["repeat_seconds"] == 86400.0


def test_add_rejects_a_period_it_cannot_parse(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add", "--at", "+1h", "--every", "fortnightly", "--task", "x"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "wake: error" in out.err


def test_add_refuses_a_period_on_rtcwake(capsys: pytest.CaptureFixture[str]) -> None:
    """rtcwake is armed here and now; no scheduler ever sees it again to re-arm."""
    code = main(
        ["add", "--at", "+1h", "--every", "1d", "--backend", "rtcwake", "--task", "x"]
    )
    assert code == 1
    assert "rtcwake" in capsys.readouterr().err


def test_list_shows_the_period_and_the_owner(capsys: pytest.CaptureFixture[str]) -> None:
    """Both are silent failure modes when invisible."""
    main(["add", "--at", "+1h", "--every", "12h", "--on", "laptop", "--task", "x"])
    capsys.readouterr()
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "every" in out and "12h" in out
    assert "on" in out and "laptop" in out


def test_list_names_the_server_for_an_unassigned_task(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["add", "--at", "+1h", "--task", "x"])
    capsys.readouterr()
    main(["list"])
    assert "server" in capsys.readouterr().out


# -- the wol default target -------------------------------------------------


def test_wol_falls_back_to_the_configured_mac(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved at add time so the row carries a MAC the firing machine can use."""
    monkeypatch.setenv("WAKE_MAC", "00:00:5e:00:53:2a")
    assert main(["add", "--at", "+1h", "--backend", "wol", "--task", "wol"]) == 0
    capsys.readouterr()
    main(["list", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["target"] == "00:00:5e:00:53:2a"


def test_an_explicit_target_still_wins(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAKE_MAC", "00:00:5e:00:53:2a")
    main([
        "add", "--at", "+1h", "--backend", "wol",
        "--target", "00:00:5e:00:53:ff", "--task", "wol",
    ])
    capsys.readouterr()
    main(["list", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["target"] == "00:00:5e:00:53:ff"


def test_wol_with_no_mac_anywhere_says_where_to_put_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["add", "--at", "+1h", "--backend", "wol", "--task", "wol"]) == 1
    assert "MAC" in capsys.readouterr().err
