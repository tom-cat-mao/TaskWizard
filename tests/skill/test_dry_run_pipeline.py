"""End-to-end offline smoke for the diagnosis pipeline.

``run_diagnosis.py dry-run`` writes a synthetic runner IPC run dir (events /
control / run.json / spec.json) plus diagnostic evidence and one real PNG, then
runs the normal analyze + report path — no device, no model, no network. This
asserts the offline pipeline is intact end to end and the report is safe.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import run_diagnosis


def _run_dir(tmp_path: Path) -> Path:
    dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(dirs) == 1, f"expected exactly one run dir, got {dirs}"
    return dirs[0]


def test_dry_run_pipeline(tmp_path, capsys):
    rc = run_diagnosis.main(["dry-run", "--output-dir", str(tmp_path), "--quiet"])
    assert rc == 0

    run_dir = _run_dir(tmp_path)
    for name in ("events.jsonl", "evidence.jsonl", "run.json", "spec.json", "summary.json", "report.html", "status.json", "case.json"):
        assert (run_dir / name).exists(), f"missing artifact: {name}"

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert events
    kinds = {e["event"] for e in events}
    assert {"model_call", "tool_call", "tool_result", "run_end"} <= kinds

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"] == "success"
    assert summary["harness_terminal"]["state"] == "succeeded"
    # The three layers are present and independent.
    assert summary["case_acceptance"]["overall"] == "pass"
    assert summary["diagnosis"]["inference"] is True
    # requested/actual are separated; missing cache stays null.
    assert summary["model"]["requested_models"] == ["synthetic-primary"]
    assert summary["model"]["actual_models"] == ["synthetic-backup"]
    assert summary["model"]["token_usage"]["cache_read_tokens"] is None
    # real fallback event recovered from the trace.
    assert summary["fallback"] and summary["fallback"][0]["actual"] == "synthetic-backup"
    # dry-run disclaimer recorded.
    assert any("dry-run" in note for note in summary.get("notes", []))

    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert len(html) > 5000
    assert "data:image" not in html
    assert "https://" not in html  # offline: no CDN / remote assets
    assert "三层裁定" in html and "Case 验收" in html and "逐步回放" in html
    assert "screenshots/screen-1.png" in html
    assert summary.get("replay"), "summary must carry a per-step replay list"

    # private artifacts
    for name in ("summary.json", "report.html", "evidence.jsonl", "case.json"):
        assert stat.S_IMODE((run_dir / name).stat().st_mode) == 0o600

    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "completed"
    assert status["run_end_seen"] is True

    assert capsys.readouterr().out.strip() == ""


def test_analyze_and_report_subcommands(tmp_path):
    rc = run_diagnosis.main(["dry-run", "--output-dir", str(tmp_path), "--quiet"])
    assert rc == 0
    run_dir = _run_dir(tmp_path)

    rc = run_diagnosis.main(["analyze", str(run_dir), "--quiet"])
    assert rc == 0
    resummary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert resummary["verdict"] == "success"

    rc = run_diagnosis.main(["report", str(run_dir)])
    assert rc == 0
    assert "data:image" not in (run_dir / "report.html").read_text(encoding="utf-8")


def test_report_is_render_only_and_preserves_metadata(tmp_path):
    rc = run_diagnosis.main(["dry-run", "--output-dir", str(tmp_path), "--quiet"])
    assert rc == 0
    run_dir = _run_dir(tmp_path)
    before = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    # Mutate a marker field the analyzer would overwrite; report must not touch it.
    before["created_at"] = "MANUAL-CREATED"
    before["duration_sec"] = 12.5
    before["case_acceptance"]["marker"] = "keep-me"
    (run_dir / "summary.json").write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")

    rc = run_diagnosis.main(["report", str(run_dir / "summary.json")])
    assert rc == 0
    after = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert after == before  # report is render-only


def test_analyze_preserves_run_metadata(tmp_path):
    rc = run_diagnosis.main(["dry-run", "--output-dir", str(tmp_path), "--quiet"])
    assert rc == 0
    run_dir = _run_dir(tmp_path)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    created = summary["created_at"]
    rc = run_diagnosis.main(["analyze", str(run_dir), "--quiet"])
    assert rc == 0
    again = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert again["created_at"] == created
    assert any("dry-run" in note for note in again.get("notes", []))


def test_stop_request_is_not_an_ended_run(tmp_path):
    import run_diagnosis as rd
    from synthetic import write_synthetic_run

    run_dir = tmp_path / "live-0001"
    run_dir.mkdir()
    write_synthetic_run(run_dir, run_id="live-0001")
    # Make it non-terminal: drop the run_end event line.
    events_path = run_dir / "events.jsonl"
    keep = [
        line
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("event") != "run_end"
    ]
    events_path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    # The runner never wrote run.json in this truncated scenario.
    (run_dir / "run.json").unlink(missing_ok=True)

    rc = rd.main(["stop", str(run_dir)])
    assert rc == 0
    rc = rd.main(["analyze", str(run_dir), "--quiet"])
    assert rc == 0
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["harness_terminal"]["stop_requested"] is True
    assert summary["harness_terminal"]["run_end_seen"] is False
    assert summary["harness_terminal"]["state"] == "stopping"
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "stopping"
