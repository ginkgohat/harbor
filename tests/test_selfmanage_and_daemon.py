"""Tests for selfmanage and daemon modules."""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from harbor import daemon, selfmanage


def _unexpected_pip(*_args):
    """Fail a test if the code under test tries to run pip."""
    raise AssertionError("pip must not run in this environment")


# ---------------------------------------------------------------------------
# selfmanage tests
# ---------------------------------------------------------------------------


class TestRunPip:
    def test_runs_current_interpreter_and_captures_output(self):
        completed = subprocess.CompletedProcess([], 0, "ok\n", "")
        with patch("subprocess.run", return_value=completed) as mock_run:
            proc = selfmanage._run_pip(["install", "foo"])
        assert proc.returncode == 0
        # First three args should be the current Python interpreter and -m pip
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1] == "-m"
        assert cmd[2] == "pip"
        assert cmd[3:] == ["install", "foo"]
        # Output must be captured so callers can inspect it (PEP 668, etc.).
        assert mock_run.call_args.kwargs == {
            "capture_output": True,
            "text": True,
            "check": False,
        }

    def test_returns_nonzero_on_failure(self):
        completed = subprocess.CompletedProcess([], 1, "", "boom")
        with patch("subprocess.run", return_value=completed):
            proc = selfmanage._run_pip(["uninstall", "foo"])
        assert proc.returncode == 1
        assert proc.stderr == "boom"

    def test_file_not_found(self, capsys):
        with patch("subprocess.run", side_effect=FileNotFoundError("pip")):
            proc = selfmanage._run_pip(["list"])
        assert proc.returncode == 1
        captured = capsys.readouterr()
        assert "pip is not available" in captured.err


class TestDetectInstallKind:
    def test_editable_when_git_dir_present(self, tmp_path):
        # Create a fake harbor package inside a git repo.
        pkg_dir = tmp_path / "src" / "harbor"
        pkg_dir.mkdir(parents=True)
        (tmp_path / ".git").mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "selfmanage.py").write_text("")

        # Monkey-patch __file__ to point into our fake package.
        real_file = selfmanage.__file__
        try:
            selfmanage.__file__ = str(pkg_dir / "selfmanage.py")
            kind = selfmanage._detect_install_kind()
            assert kind == "editable"
        finally:
            selfmanage.__file__ = real_file


class TestEnvironmentDetection:
    """Pure detection helpers: no subprocesses, no real environment needed."""

    def test_pep668_marker_present(self, tmp_path):
        (tmp_path / selfmanage.PEP668_MARKER).write_text("[externally-managed]\n")
        assert selfmanage._has_externally_managed_marker(str(tmp_path)) is True

    def test_pep668_marker_absent(self, tmp_path):
        assert selfmanage._has_externally_managed_marker(str(tmp_path)) is False

    def test_pep668_marker_empty_stdlib_dir(self):
        assert selfmanage._has_externally_managed_marker("") is False

    def test_system_interpreter_with_marker_is_externally_managed(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / selfmanage.PEP668_MARKER).write_text("")
        monkeypatch.setattr(selfmanage, "_running_under_virtualenv", lambda: False)
        monkeypatch.setattr(
            selfmanage.sysconfig, "get_path", lambda name: str(tmp_path)
        )
        assert selfmanage._is_externally_managed() is True

    def test_virtualenv_is_never_externally_managed(self, tmp_path, monkeypatch):
        # The base interpreter's stdlib may carry the marker (Homebrew does),
        # but pip ignores it inside a venv and so must we.
        (tmp_path / selfmanage.PEP668_MARKER).write_text("")
        monkeypatch.setattr(selfmanage, "_running_under_virtualenv", lambda: True)
        monkeypatch.setattr(
            selfmanage.sysconfig, "get_path", lambda name: str(tmp_path)
        )
        assert selfmanage._is_externally_managed() is False

    @pytest.mark.parametrize(
        "prefix",
        [
            "/home/dev/.local/pipx/venvs/harbor",
            "/Users/dev/pipx/venvs/harbor",
            "C:\\Users\\dev\\AppData\\Local\\pipx\\venvs\\harbor",
            "/home/dev/PipX/Venvs/Harbor",
        ],
    )
    def test_pipx_venv_prefixes_are_detected(self, prefix):
        assert selfmanage._looks_like_pipx_venv(prefix) is True

    @pytest.mark.parametrize(
        "prefix",
        [
            "/usr",
            "/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14",
            "/Users/dev/.venvs/harbor",
            "/home/dev/.local/pipx/harbor",
            "/home/dev/pipx/venvs-other/harbor",
        ],
    )
    def test_non_pipx_prefixes_are_not_detected(self, prefix):
        assert selfmanage._looks_like_pipx_venv(prefix) is False

    def test_is_pipx_managed_reads_sys_prefix(self, monkeypatch):
        monkeypatch.setattr(
            selfmanage.sys, "prefix", "/home/dev/.local/pipx/venvs/harbor"
        )
        assert selfmanage._is_pipx_managed() is True

    def test_is_pipx_managed_false_for_regular_venv(self, monkeypatch):
        monkeypatch.setattr(selfmanage.sys, "prefix", "/home/dev/.venvs/harbor")
        assert selfmanage._is_pipx_managed() is False


class TestCmdSelfUpdate:
    def test_editable_mode_rejects_update(self, capsys):
        with patch.object(selfmanage, "_detect_install_kind", return_value="editable"):
            rc = selfmanage.cmd_self_update()
            assert rc == 1
            captured = capsys.readouterr()
            assert "editable (dev) mode" in captured.err

    def test_git_install_runs_pip_upgrade(self, capsys, monkeypatch):
        monkeypatch.setattr(selfmanage, "_detect_install_kind", lambda: "git+https")
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: False)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(selfmanage, "_run_pip", return_value=completed) as mock_run:
            rc = selfmanage.cmd_self_update()
        assert rc == 0
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "install" in args
        assert "--upgrade" in args
        assert selfmanage.GITHUB_INSTALL_URL in args
        captured = capsys.readouterr()
        assert "updated" in captured.out

    def test_update_failure_returns_one(self, capsys, monkeypatch):
        monkeypatch.setattr(selfmanage, "_detect_install_kind", lambda: "git+https")
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: False)
        completed = subprocess.CompletedProcess([], 1, "", "boom\n")
        with patch.object(selfmanage, "_run_pip", return_value=completed):
            rc = selfmanage.cmd_self_update()
        assert rc == 1
        captured = capsys.readouterr()
        assert "failed" in captured.err

    def test_prints_captured_pip_output_on_success(self, capsys, monkeypatch):
        monkeypatch.setattr(selfmanage, "_detect_install_kind", lambda: "git+https")
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: False)
        completed = subprocess.CompletedProcess(
            [], 0, "Successfully installed harbor-0.1.2\n", ""
        )
        with patch.object(selfmanage, "_run_pip", return_value=completed):
            rc = selfmanage.cmd_self_update()
        assert rc == 0
        captured = capsys.readouterr()
        assert "Successfully installed harbor-0.1.2" in captured.out
        assert "updated" in captured.out

    def test_pipx_managed_update_recommends_pipx(self, capsys, monkeypatch):
        monkeypatch.setattr(selfmanage, "_detect_install_kind", lambda: "git+https")
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: True)
        monkeypatch.setattr(selfmanage, "_run_pip", _unexpected_pip)
        rc = selfmanage.cmd_self_update()
        assert rc == 1
        err = capsys.readouterr().err
        assert selfmanage.PIPX_UPGRADE_COMMAND in err
        assert "pipx" in err

    def test_pep668_update_prints_pipx_and_venv_guidance(self, capsys, monkeypatch):
        monkeypatch.setattr(selfmanage, "_detect_install_kind", lambda: "git+https")
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: True)
        monkeypatch.setattr(selfmanage, "_run_pip", _unexpected_pip)
        rc = selfmanage.cmd_self_update()
        assert rc == 1
        err = capsys.readouterr().err
        assert "externally managed" in err
        assert selfmanage.PIPX_INSTALL_COMMAND in err
        assert "python3 -m venv" in err
        assert "--break-system-packages" in err

    def test_pip_failure_with_pep668_output_gives_guidance(self, capsys, monkeypatch):
        # pip refused even though the pre-check passed: the captured output
        # must drive the guidance, not just the pre-check.
        monkeypatch.setattr(selfmanage, "_detect_install_kind", lambda: "git+https")
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: False)
        completed = subprocess.CompletedProcess(
            [], 1, "", "error: externally-managed-environment\n"
        )
        with patch.object(selfmanage, "_run_pip", return_value=completed):
            rc = selfmanage.cmd_self_update()
        assert rc == 1
        err = capsys.readouterr().err
        assert "Update failed" in err
        assert "python3 -m venv" in err

    def test_pip_failure_no_matching_distribution_mentions_network(
        self, capsys, monkeypatch
    ):
        monkeypatch.setattr(selfmanage, "_detect_install_kind", lambda: "git+https")
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: False)
        completed = subprocess.CompletedProcess(
            [], 1, "ERROR: No matching distribution found for harbor\n", ""
        )
        with patch.object(selfmanage, "_run_pip", return_value=completed):
            rc = selfmanage.cmd_self_update()
        assert rc == 1
        err = capsys.readouterr().err
        assert "github.com" in err

    def test_pip_failure_generic_suggests_pipx(self, capsys, monkeypatch):
        monkeypatch.setattr(selfmanage, "_detect_install_kind", lambda: "git+https")
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: False)
        completed = subprocess.CompletedProcess([], 1, "", "boom\n")
        with patch.object(selfmanage, "_run_pip", return_value=completed):
            rc = selfmanage.cmd_self_update()
        assert rc == 1
        err = capsys.readouterr().err
        assert selfmanage.PIPX_UPGRADE_COMMAND in err


class TestCmdSelfUninstall:
    def test_non_tty_no_confirmation_and_runs_pip(self, capsys, monkeypatch):
        # sys.stdin.isatty() returns False in test runner.
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: False)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(selfmanage, "_run_pip", return_value=completed) as mock_run:
            rc = selfmanage.cmd_self_uninstall()
        assert rc == 0
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "uninstall" in args
        assert "-y" in args
        assert "harbor" in args
        captured = capsys.readouterr()
        assert "uninstalled" in captured.out

    def test_uninstall_failure_returns_one(self, capsys, monkeypatch):
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: False)
        completed = subprocess.CompletedProcess([], 1, "", "boom\n")
        with patch.object(selfmanage, "_run_pip", return_value=completed):
            rc = selfmanage.cmd_self_uninstall()
        assert rc == 1
        captured = capsys.readouterr()
        assert "failed" in captured.err

    def test_interactive_decline_cancels(self, capsys, monkeypatch):
        class _TTYStdin:
            def isatty(self):
                return True

        monkeypatch.setattr(selfmanage.sys, "stdin", _TTYStdin())
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_run_pip", _unexpected_pip)
        rc = selfmanage.cmd_self_uninstall()
        assert rc == 0
        captured = capsys.readouterr()
        assert "cancelled" in captured.out

    def test_pipx_managed_uninstall_recommends_pipx(self, capsys, monkeypatch):
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: True)
        monkeypatch.setattr(selfmanage, "_run_pip", _unexpected_pip)
        rc = selfmanage.cmd_self_uninstall()
        assert rc == 1
        err = capsys.readouterr().err
        assert selfmanage.PIPX_UNINSTALL_COMMAND in err

    def test_pep668_uninstall_guidance(self, capsys, monkeypatch):
        monkeypatch.setattr(selfmanage, "_is_pipx_managed", lambda: False)
        monkeypatch.setattr(selfmanage, "_is_externally_managed", lambda: True)
        monkeypatch.setattr(selfmanage, "_run_pip", _unexpected_pip)
        rc = selfmanage.cmd_self_uninstall()
        assert rc == 1
        err = capsys.readouterr().err
        assert "externally managed" in err
        assert selfmanage.PIPX_UNINSTALL_COMMAND in err
        assert "--break-system-packages" in err


# ---------------------------------------------------------------------------
# daemon tests
# ---------------------------------------------------------------------------


class TestPIDHelpers:
    def test_read_pid_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "nope.pid")
        assert daemon._read_pid() is None

    def test_read_pid_with_valid_content(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "harbor.pid"
        pid_file.write_text("12345")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        assert daemon._read_pid() == 12345

    def test_read_pid_with_empty_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "harbor.pid"
        pid_file.write_text("")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        assert daemon._read_pid() is None

    def test_read_pid_with_junk(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "harbor.pid"
        pid_file.write_text("not-a-number")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        assert daemon._read_pid() is None

    def test_write_pid_creates_state_dir(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        pid_file = state_dir / "harbor.pid"
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        daemon._write_pid(9999)
        assert pid_file.is_file()
        assert pid_file.read_text() == "9999"

    def test_remove_pid_no_file_is_noop(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "harbor.pid"
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        # Should not raise.
        daemon._remove_pid()

    def test_remove_pid_deletes_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "harbor.pid"
        pid_file.write_text("1")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        daemon._remove_pid()
        assert not pid_file.exists()


class TestIsProcessAlive:
    def test_current_process_is_alive(self):
        assert daemon._is_process_alive(os.getpid()) is True

    def test_impossibly_high_pid_is_dead(self):
        # PIDs are bounded; 999999 should never exist.
        assert daemon._is_process_alive(999999) is False


class TestPIDStartTime:
    def test_current_process_does_not_raise(self):
        # _pid_start_time should never crash; the result may be None on
        # platforms or locales where ps output can't be parsed.
        t = daemon._pid_start_time(os.getpid())
        assert t is None or t > 0


class TestCmdStatus:
    def test_no_pid_file(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "nope.pid")
        rc = daemon.cmd_status()
        assert rc == 1
        captured = capsys.readouterr()
        assert "not running" in captured.out

    def test_stale_pid_file(self, capsys, tmp_path, monkeypatch):
        pid_file = tmp_path / "harbor.pid"
        pid_file.write_text("999999")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        rc = daemon.cmd_status()
        assert rc == 1
        # Stale PID file should be cleaned up.
        assert not pid_file.exists()
        captured = capsys.readouterr()
        assert "stale" in captured.out

    def test_running_process(self, capsys, tmp_path, monkeypatch):
        pid_file = tmp_path / "harbor.pid"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "harbor.log")
        rc = daemon.cmd_status()
        assert rc == 0
        captured = capsys.readouterr()
        assert "running" in captured.out


class TestCmdStop:
    def test_no_pid_file(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "nope.pid")
        rc = daemon.cmd_stop()
        assert rc == 1
        captured = capsys.readouterr()
        assert "not running" in captured.out

    def test_stale_pid_file(self, capsys, tmp_path, monkeypatch):
        pid_file = tmp_path / "harbor.pid"
        pid_file.write_text("999999")
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        rc = daemon.cmd_stop()
        assert rc == 1
        assert not pid_file.exists()


class TestCmdStart:
    def test_no_fork_platform_rejects(self, capsys):
        # Simulate a platform without fork by patching hasattr inside daemon.
        with (
            patch.object(daemon.os, "fork", create=True),
            patch("builtins.hasattr", return_value=False),
        ):
            rc = daemon.cmd_start(MagicMock())
            assert rc == 1
            captured = capsys.readouterr()
            assert "not supported" in captured.err

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork (Unix-only)")
    def test_already_running_returns_zero(self, capsys, tmp_path, monkeypatch):
        pid_file = tmp_path / "harbor.pid"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr(daemon, "PID_FILE", pid_file)
        monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "harbor.log")
        rc = daemon.cmd_start(MagicMock())
        assert rc == 0
        captured = capsys.readouterr()
        assert "already running" in captured.out


# ---------------------------------------------------------------------------
# CLI / argparse tests (top-level and subcommand dispatch)
# ---------------------------------------------------------------------------


class TestCLI:
    def test_help_lists_subcommands(self):
        from harbor.__main__ import _build_parser

        parser = _build_parser()
        choices = list(parser._subparsers._group_actions[0].choices.keys())
        for name in ["serve", "update", "uninstall", "start", "status", "stop"]:
            assert name in choices
