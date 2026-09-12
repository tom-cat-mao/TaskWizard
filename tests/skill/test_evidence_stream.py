"""Evidence-stream selection, JSONL issues, synthetic isolation."""

from __future__ import annotations

import json
from pathlib import Path

import run_diagnosis as rd
from events import read_jsonl_with_issues
from synthetic import write_synthetic_run


def test_find_evidence_prefers_producer_over_stale_copy(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "evidence.jsonl").write_text('{"event":"run_start"}\n', encoding="utf-8")
    producer = run_dir / "live-1.evidence.jsonl"
    producer.write_text(
        '{"event":"run_start"}\n{"event":"run_end"}\n', encoding="utf-8"
    )
    found = rd._find_evidence(run_dir, "live-1")
    assert found == producer


def test_analyze_reads_growing_producer(tmp_path):
    run_dir = tmp_path / "grow"
    run_dir.mkdir()
    write_synthetic_run(run_dir, run_id="grow")
    producer = run_dir / "grow.evidence.jsonl"
    before = len(producer.read_text(encoding="utf-8").splitlines())
    with producer.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"event": "tool_observation", "step": 99, "tool": "tap", "result_text": "OK. tapped", "image": {"present": False}})
            + "\n"
        )
    rd.main(["analyze", str(run_dir), "--quiet"])
    # The derived stable copy now reflects the producer (copy is derived, not source).
    stable = (run_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(stable) == before + 1


def test_jsonl_issues_are_reported(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    write_synthetic_run(run_dir, run_id="r")
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
        handle.write('["not","an","object"]\n')
    rc = rd.main(["analyze", str(run_dir), "--quiet"])
    assert rc == 0
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    reasons = {issue["reason"] for issue in summary["data_issues"]}
    assert "invalid_json" in reasons
    assert "not_an_object" in reasons
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "JSONL 解析问题" in html


def test_read_jsonl_with_issues_bounded():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.jsonl"
        p.write_text("bad\n" * 50, encoding="utf-8")
        rows, issues = read_jsonl_with_issues(p, source="x.jsonl")
        assert rows == []
        assert len(issues) == 20  # bounded


def test_dry_run_uses_unique_dirs_and_does_not_delete(tmp_path):
    assert rd.main(["dry-run", "--output-dir", str(tmp_path), "--quiet"]) == 0
    assert rd.main(["dry-run", "--output-dir", str(tmp_path), "--quiet"]) == 0
    dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(dirs) == 2
    for run_dir in dirs:
        assert (run_dir / "summary.json").exists()


def test_dry_run_rejects_case_flag(tmp_path):
    # dry-run always uses the synthetic Case; a user Case cannot be substituted.
    import pytest

    with pytest.raises(SystemExit):
        rd.main(["dry-run", "--case", "x.json", "--output-dir", str(tmp_path)])


def _minimal_run(run_dir: Path, run_id: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    events = [
        {"event": "model_call", "step": 1, "ts": 100.0},
        {"event": "run_end", "status": "succeeded", "result": {"success": True, "reason": "done", "steps": 1}, "ts": 110.0},
    ]
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8"
    )
    (run_dir / "control.jsonl").write_text("", encoding="utf-8")
    (run_dir / "launch.json").write_text(json.dumps({"run_id": run_id, "task": "t"}), encoding="utf-8")


def test_analyze_ambiguous_producers_fail_closed(tmp_path, capsys):
    run_dir = tmp_path / "run"
    _minimal_run(run_dir, "run")
    (run_dir / "a.evidence.jsonl").write_text('{"event":"run_start"}\n', encoding="utf-8")
    (run_dir / "b.evidence.jsonl").write_text('{"event":"run_start"}\n', encoding="utf-8")
    rc = rd.main(["analyze", str(run_dir), "--quiet"])
    assert rc == 1
    assert "multiple producer" in capsys.readouterr().err


def test_analyze_explicit_evidence_file_is_exact(tmp_path):
    run_dir = tmp_path / "run"
    _minimal_run(run_dir, "run")
    chosen = run_dir / "a.evidence.jsonl"
    other = run_dir / "b.evidence.jsonl"
    chosen.write_text(
        json.dumps({"event": "tool_observation", "step": 1, "tool": "read_screen", "result_text": "CHOSEN-MARKER", "image": {"present": False}})
        + "\n",
        encoding="utf-8",
    )
    other.write_text(
        json.dumps({"event": "tool_observation", "step": 1, "tool": "read_screen", "result_text": "OTHER-MARKER", "image": {"present": False}})
        + "\n",
        encoding="utf-8",
    )
    rc = rd.main(["analyze", str(chosen), "--quiet"])
    assert rc == 0
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    replay_text = json.dumps(summary["replay"], ensure_ascii=False)
    assert "CHOSEN-MARKER" in replay_text


def test_duration_is_recomputed_from_growing_events(tmp_path):
    run_dir = tmp_path / "run"
    _minimal_run(run_dir, "run")
    assert rd.main(["analyze", str(run_dir), "--quiet"]) == 0
    first = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert first["duration_sec"] == 10.0
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "model_call", "step": 2, "ts": 130.0}) + "\n")
    assert rd.main(["analyze", str(run_dir), "--quiet"]) == 0
    second = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert second["duration_sec"] == 30.0


def test_bad_diagnostic_tail_is_reported(tmp_path):
    run_dir = tmp_path / "run"
    _minimal_run(run_dir, "run")
    producer = run_dir / "run.evidence.jsonl"
    producer.write_text('{"event":"run_start"}\n{not-json\n', encoding="utf-8")
    assert rd.main(["analyze", str(run_dir), "--quiet"]) == 0
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    sources = {(i["source"], i["reason"]) for i in summary["data_issues"]}
    assert any("invalid_json" == reason and "run.evidence.jsonl" in source for source, reason in sources)
