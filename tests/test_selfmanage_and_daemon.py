"""Tests for selfmanage and daemon modules."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from harbor import daemon, selfmanage

# ---------------------------------------------------------------------------
# selfmanage tests
# ---------------------------------------------------------------------------

class TestRunPip:
    def test_returns_zero_on_success(self):
        with patch("subprocess.call", return_value=0) as mock_call:
            rc = selfmanage._run_pip(["install", "foo"])
            assert rc == 0
            mock_call.assert_called_once()
            # First two args should be the current Python interpreter and -m pip
            cmd = mock_call.call_args[0][0]
            assert cmd[0] == sys.executable
            assert cmd[1] == "-m"
            assert cmd[2] == "pip"
            assert cmd[3:] == ["install", "foo"]

    def test_returns_nonzero_on_failure(self):
        with patch("subprocess.call", return_value=1):
            rc = selfmanage._run_pip(["uninstall", "foo"])
            assert rc == 1

    def test_file_not_found(self, capsys):
        with patch("subprocess.call", side_effect=FileNotFoundError("pip")):
            rc = selfmanage._run_pip(["list"])
            assert rc == 1
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


class TestCmdSelfUpdate:
    def test_editable_mode_rejects_update(self, capsys):
        with patch.object(selfmanage, "_detect_install_kind", return_value="editable"):
            rc = selfmanage.cmd_self_update()
            assert rc == 1
            captured = capsys.readouterr()
            assert "editable (dev) mode" in captured.err

    def test_git_install_runs_pip_upgrade(self, capsys):
        with patch.object(selfmanage, "_detect_install_kind", return_value="git+https"), \
             patch.object(selfmanage, "_run_pip", return_value=0) as mock_run:
            rc = selfmanage.cmd_self_update()
            assert rc == 0
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "install" in args
            assert "--upgrade" in args
            assert selfmanage.GITHUB_INSTALL_URL in args
            captured = capsys.readouterr()
            assert "updated" in captured.out

    def test_update_failure_returns_one(self, capsys):
        with patch.object(selfmanage, "_detect_install_kind", return_value="git+https"), \
             patch.object(selfmanage, "_run_pip", return_value=1):
            rc = selfmanage.cmd_self_update()
            assert rc == 1
            captured = capsys.readouterr()
            assert "failed" in captured.err


class TestCmdSelfUninstall:
    def test_non_tty_no_confirmation_and_runs_pip(self, capsys):
        # sys.stdin.isatty() returns False in test runner.
        with patch.object(selfmanage, "_run_pip", return_value=0) as mock_run:
            rc = selfmanage.cmd_self_uninstall()
            assert rc == 0
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "uninstall" in args
            assert "-y" in args
            assert "harbor" in args
            captured = capsys.readouterr()
            assert "uninstalled" in captured.out

    def test_uninstall_failure_returns_one(self, capsys):
        with patch.object(selfmanage, "_run_pip", return_value=1):
            rc = selfmanage.cmd_self_uninstall()
            assert rc == 1
            captured = capsys.readouterr()
            assert "failed" in captured.err


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

    @pytest.mark.skipif(
        not hasattr(os, "fork"), reason="requires fork (Unix-only)",
    )
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
