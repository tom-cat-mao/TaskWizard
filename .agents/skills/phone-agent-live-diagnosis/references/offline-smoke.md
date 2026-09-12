# Offline smoke

The offline smoke needs no device, no model, and no network. It materializes a
synthetic run directory with the **same IPC file shapes the runner produces**,
plus a diagnostic evidence stream and one real (tiny) PNG, then runs the normal
analyze + report path.

```bash
.venv/bin/python .agents/skills/phone-agent-live-diagnosis/scripts/run_diagnosis.py dry-run
```

Artifacts land under a **unique new** `outputs/live-diagnosis/synthetic-<ts>-<id>/`
directory (override the parent with `--output-dir`). It never deletes existing
data and reads no local config or global memory. It always uses the built-in
synthetic Case (`--case` is not accepted). It writes:

- `events.jsonl`, `control.jsonl`, `run.json`, `spec.json` — synthetic IPC;
- `synthetic-0001.evidence.jsonl` + `evidence.jsonl` — diagnostic stream;
- `screenshots/screen-1.png` — a real 1×1 PNG;
- `traces/synthetic-0001.jsonl` — a synthetic `model_fallback` event;
- `summary.json`, `report.html`, `status.json`, `case.json`.

What it proves: the analyze → report pipeline runs end to end offline; the
synthetic Case evaluates to `pass`; requested/actual model identity is
separated; missing cache usage stays `null`; the report is base64-free, has no
remote assets, and references only the on-disk screenshot.

What it does **not** prove: real grounding, real finish/verifier semantics,
device behavior, or any gateway interaction. The report records this disclaimer.

The unit tests for the pipeline live in `tests/skill/`:

```bash
.venv/bin/python -m pytest tests/skill -q
```
