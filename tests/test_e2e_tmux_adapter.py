from __future__ import annotations

import shutil
import time
import uuid

import pytest

from bemine.tmux_adapter import TmuxAdapter


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_tmux_adapter_can_start_send_capture_and_kill() -> None:
    tmux = TmuxAdapter()
    name = f"bemine-test-{uuid.uuid4().hex[:10]}"
    try:
        tmux.start(name)
        assert tmux.exists(name)
        tmux.send(name, "printf 'BEMINE_READY\\n'")
        deadline = time.time() + 5
        captured = ""
        while time.time() < deadline:
            captured = tmux.capture(name)
            if "BEMINE_READY" in captured:
                break
            time.sleep(0.1)
        assert "BEMINE_READY" in captured
    finally:
        tmux.kill(name)
        assert not tmux.exists(name)

