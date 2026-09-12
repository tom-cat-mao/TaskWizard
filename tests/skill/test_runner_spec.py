"""Offline checks that the driver reuses the formal runner IPC / config stack.

No device, no model, no network: these build the resolved config and the
``RunSpec`` the way a real run would, then verify the on-disk IPC wiring.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_diagnosis import _build_spec, _overrides_from_args


def _args(**overrides) -> argparse.Namespace:
    base = {
        "device_id": None,
        "max_steps": None,
        "token_budget": None,
        "base_url": None,
        "model": None,
        "apikey": None,
        "model_timeout": None,
        "model_max_retries": None,
        "grounding_provider": None,
        "accessibility_timeout": None,
        "accessibility_max_marks": None,
        "locateanything_model": None,
        "locateanything_max_size": None,
        "lang": None,
        "no_taskdoc": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_overrides_force_diagnosis_evidence_into_run_dir(tmp_path):
    run_dir = tmp_path / "r1"
    overrides = _overrides_from_args(_args(), run_dir)
    assert overrides["diagnostic_evidence"] is True
    assert overrides["diagnostic_unredacted"] is True
    assert overrides["diagnostic_evidence_dir"] == str(run_dir)
    assert overrides["trace_dir"] == str(run_dir / "traces")
    # unset flags are dropped so env/.env values are not clobbered by None.
    assert "device_id" not in overrides or overrides["device_id"] is None


def test_build_spec_wires_formal_run_paths(tmp_path):
    from phone_agent.v2.config import V2Config

    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    config = V2Config(base_url="http://localhost:1/v1", model_name="test-model")
    built = _build_spec(config, "r1", "打开设置", run_dir)

    spec_path = Path(built["spec_path"])
    assert spec_path == run_dir / "spec.json"
    assert spec_path.exists()

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["run_id"] == "r1"
    assert spec["task"] == "打开设置"
    assert spec["events_path"].endswith("/events.jsonl")
    assert spec["control_path"].endswith("/control.jsonl")
    assert spec["snapshot"]["config_fingerprint"].startswith("sha256:")
    # the resolved config is the full dataclass field set (runner re-validates it)
    assert "safety_mode" in spec["overrides"]
