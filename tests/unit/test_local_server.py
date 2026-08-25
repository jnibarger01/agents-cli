"""Tests for local server process lifecycle safety."""

from unittest import mock

import click

from google.agents.cli.run import _local_server


def test_pid_reuse_is_not_considered_a_live_server() -> None:
    process = mock.Mock()
    process.create_time.return_value = 20.0

    with mock.patch.object(_local_server.psutil, "Process", return_value=process):
        assert not _local_server._is_server_alive(123, 18080, 10.0)


def test_legacy_pid_file_cannot_terminate_a_process(tmp_path) -> None:
    pid_file = tmp_path / ".google-agents-cli" / "run_server.json"
    pid_file.parent.mkdir()
    pid_file.write_text('{"pid": 123, "port": 18080}\n')

    with mock.patch.object(_local_server.psutil, "Process") as process:
        _local_server.stop_server(tmp_path)

    process.assert_not_called()
    assert not pid_file.exists()


def test_malformed_pid_file_is_ignored(tmp_path) -> None:
    pid_file = tmp_path / ".google-agents-cli" / "run_server.json"
    pid_file.parent.mkdir()
    pid_file.write_text('{"pid": "not-a-pid", "port": 18080}\n')

    assert _local_server._read_pid_file(tmp_path) is None


def test_pid_reuse_is_not_terminated(tmp_path) -> None:
    process = mock.Mock()
    process.create_time.return_value = 20.0
    info = {"pid": 123, "create_time": 10.0}

    with mock.patch.object(_local_server.psutil, "Process", return_value=process):
        _local_server._cleanup(tmp_path, info)

    process.terminate.assert_not_called()


def test_startup_failure_cleans_up_new_process(tmp_path) -> None:
    with (
        mock.patch.object(_local_server, "_find_free_port", return_value=18080),
        mock.patch.object(_local_server, "_start_server", return_value=123),
        mock.patch.object(_local_server, "_process_create_time", return_value=10.0),
        mock.patch.object(
            _local_server,
            "_wait_for_port",
            side_effect=click.ClickException("not ready"),
        ),
        mock.patch.object(_local_server, "_cleanup") as cleanup,
    ):
        try:
            _local_server.ensure_server(tmp_path, "app")
        except click.ClickException as exc:
            assert str(exc) == "not ready"
        else:
            raise AssertionError("ensure_server should fail when startup is not ready")

    cleanup.assert_called_once_with(tmp_path, {"pid": 123, "create_time": 10.0})
