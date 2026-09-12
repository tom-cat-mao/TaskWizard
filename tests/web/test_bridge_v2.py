"""V2 bridge features: screenshot history, step details, soft stop, usage."""

from __future__ import annotations

import time
import os
import threading

from phone_agent.v2.config import V2Config
from phone_agent.v2.run_ipc import JsonlReader, JsonlWriter, read_run_spec
from phone_agent.web.bridge import WebRunBridge


class _FakeProcess:
    def __init__(self, argv, producer):  # noqa: ANN001
        self.pid = os.getpid()
        self.returncode = None
        self._thread = threading.Thread(
            target=self._run, args=(read_run_spec(argv[-1]), producer), daemon=True
        )
        self._thread.start()

    def _run(self, spec, producer):  # noqa: ANN001
        producer(spec)
        self.returncode = 0

    def poll(self):
        return self.returncode


def _bridge(tmp_path, producer) -> WebRunBridge:
    return WebRunBridge(
        {},
        config_factory=lambda _o: V2Config(
            base_url="http://example.invalid/v1",
            model_name="fake",
            memory_dir=str(tmp_path / "memory"),
            runs_dir=str(tmp_path / "runs"),
        ),
        popen_factory=lambda argv, **_kwargs: _FakeProcess(argv, producer),
    )


def test_snapshot_exposes_screens_steps_usage(tmp_path):
    def producer(spec):
        with JsonlWriter(spec.events_path) as writer:
            for index in range(2):
                step = index + 1
                writer.put(
                    {"event": "model_call", "step": step, "latency_ms": 1, "tokens_total": step}
                )
                writer.put(
                    {
                        "event": "tool_call",
                        "step": step,
                        "tool": "tap",
                        "args": {"intent": f"步骤{index}", "target_mark_id": "ax_1@e1"},
                    }
                )
                writer.put(
                    {
                        "event": "tool_result",
                        "step": step,
                        "tool": "tap",
                        "text": f"[OBS] app=设置 screen#{step}",
                        "ok": True,
                        "latency_ms": 2,
                    }
                )
                writer.put(
                    {
                        "event": "screen",
                        "step": step,
                        "image": f"data:image/png;base64,frame{index}",
                        "current_app": "设置",
                        "screen_seq": step,
                    }
                )
            writer.put(
                {
                    "event": "run_end",
                    "status": "succeeded",
                    "result": {"success": True, "reason": "done", "steps": 2, "trace_path": None},
                    "tokens_total": 2,
                }
            )

    bridge = _bridge(tmp_path, producer)
    bridge.start("测试任务")
    assert bridge.wait(timeout=10)
    state = bridge.snapshot()
    assert state["status"] == "succeeded"
    assert len(state["screens"]) == 2
    assert state["screens"][0]["seq"] == 1
    assert len(state["steps"]) == 2
    assert state["steps"][0]["args"]["target_mark_id"] == "ax_1@e1"
    assert "usage" in state


def test_soft_stop_sets_takeover_channel(tmp_path):
    def producer(spec):
        reader = JsonlReader(spec.control_path)
        while True:
            if any(message.get("type") == "stop" for message in reader.read_new()):
                with JsonlWriter(spec.events_path) as writer:
                    writer.put({"event": "stopping", "step": 0})
                    writer.put(
                        {
                            "event": "run_end",
                            "status": "takeover",
                            "result": {
                                "success": False,
                                "reason": "用户从 Web 控制台停止",
                                "steps": 0,
                                "trace_path": None,
                            },
                            "tokens_total": 0,
                        }
                    )
                return
            time.sleep(0.01)

    bridge = _bridge(tmp_path, producer)
    bridge.start("停止测试")
    assert bridge.request_stop() is True
    assert bridge.wait(timeout=10)
    state = bridge.snapshot()
    assert state["status"] == "takeover"


def test_request_stop_without_run_is_noop(tmp_path):
    bridge = _bridge(tmp_path, lambda _spec: None)
    assert bridge.request_stop() is False


def test_kb_entries_empty_without_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bridge = _bridge(tmp_path, lambda _spec: None)
    assert bridge.kb_entries() == []
    assert bridge.run_dream()["status"] == "skipped"
