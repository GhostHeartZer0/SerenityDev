import os
import sys
from unittest.mock import MagicMock, Mock, patch

workspace_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if workspace_dir not in sys.path:
    sys.path.insert(0, workspace_dir)


def test_free_port_unix_kills_stale_processes_and_skips_current_process():
    import serenitydevserver

    command_result = Mock(returncode=0, stdout="123\n456\n")
    with (
        patch.object(serenitydevserver.os, "name", "posix"),
        patch("urllib.request.urlopen", side_effect=OSError),
        patch.object(serenitydevserver.subprocess, "run", return_value=command_result) as run,
        patch.object(serenitydevserver.os, "getpid", return_value=456),
        patch.object(serenitydevserver.os, "kill") as kill,
        patch.object(serenitydevserver.signal, "SIGKILL", 9, create=True),
    ):
        serenitydevserver.free_port(8002)
        kill.assert_called_once_with(123, 9)

    run.assert_called_once_with(
        "lsof -t -i:8002",
        shell=True,
        capture_output=True,
        text=True,
    )
def test_free_port_windows_kills_stale_processes_and_skips_current_process():
    import serenitydevserver

    command_result = Mock(returncode=0, stdout="  TCP    127.0.0.1:8002         0.0.0.0:0              LISTENING       1234\n  TCP    127.0.0.1:8002         0.0.0.0:0              LISTENING       5678\n")
    with (
        patch.object(serenitydevserver.os, "name", "nt"),
        patch.object(serenitydevserver.subprocess, "run", side_effect=[command_result, Mock(), Mock()]) as run,
        patch.object(serenitydevserver.os, "getpid", return_value=5678),
        patch("time.sleep")
    ):
        serenitydevserver.free_port(8002)
        assert run.call_count >= 2