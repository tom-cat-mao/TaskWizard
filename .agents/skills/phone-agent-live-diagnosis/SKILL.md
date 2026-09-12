---
name: phone-agent-live-diagnosis
description: >-
  Use when a coding agent (Pi/Codex/…) must prepare a Case, run or monitor the
  TaskWizard thin-loop (v2) PhoneAgent on a real Android device, interact with
  HITL or stop a run, and produce a trustworthy offline Chinese HTML replay plus
  a source-code diagnosis report. Trigger on 实机测试, 监看, 手机任务, Case 验收,
  live diagnosis, phone agent report, 源码归因, HTML 报告, real device run,
  trace analysis, or any request to verify what the phone agent actually did.
when_to_use: |
  Real-device runs of the v2 thin-loop PhoneAgent, Case-driven acceptance, HITL
  interaction / stop, and offline HTML diagnosis reports. Do not use for pure
  unit tests, benchmark-only LocateAnything evaluation, or code edits without a
  diagnosis request.
---

# Phone Agent Live Diagnosis (thin-loop v2)

Drive the **formal runner** for a real-device run, derive state from the real
event stream, and render an offline Chinese HTML report that keeps harness facts,
Case acceptance, and diagnostic inference apart. No device is touched by the
skill itself: it launches `python -m phone_agent.runner` and reads its IPC files.

## Mental model — a loop, not a graph

v2 is a thin loop: one model call per step on LangChain `create_agent`. There are
no nodes. Behavior lives in tools (result strings, fail-closed), the two-step
`finish` + independent verifier, the TaskDoc board, and event-bus middleware
listeners. Diagnose by mapping a symptom to one of those owners — never to a v1
node.

Marks-first: `tap` binds a unique mark (`target_mark_id` or a unique
`target_description`), fail-closed on ambiguity; raw coords only for `swipe`.
0-1000 → pixel conversion happens inside tools only.

## Three judgments — never collapse them

1. **Harness terminal (fact).** Did the run *end*, and how? Derived only from
   real events (`run_end` / `run.json`): `succeeded | takeover | stopped |
   token_budget_exhausted | loop_fuse | error | failed | uncertain`. Only the IPC
   `run_end` **event** is terminal; a `run.json` is a recorded summary shown
   separately and is never reported as `run_end_seen`. A *requested* stop
   (`control.jsonl {"type":"stop"}`) is **not** an ended run. The runner process
   exit code is never treated as success.
2. **Case acceptance (objective evidence).** The Case's checkpoints are judged
   **only** by objective device observations (`[OBS]` lines / successful
   perception-locate returns). TaskDoc evidence notes/facts and the actor's
   `finish` self-claim are candidate evidence only and **never auto-pass**. The
   goal text, per-step `intent`/`note`/`target_description`, fail-closed failure
   receipts, and unverified reference frames are **not** acceptance evidence.
   A match and a contradiction together are a conflict → `unknown`. Missing
   evidence → `unknown`. `finished` never implies Case `pass`; an unresolved HITL
   is never auto-approved.
3. **Diagnosis (inference).** Source-mapped candidate causes for a human to
   confirm. A `path:line` anchor is *where a symbol lives*, not a proven root
   cause. A single tool error is a tool-health fact, not a whole-run failure.

## Case

A Case is plain JSON (see `references/case-format.md`): `goal`, `preconditions`,
`acceptance[]` (id / description / optional `match` / `contradict`), and
`safety_boundaries[]`.

- `acceptance` matching is intentionally dumb and auditable and is scoped to
  **objective** evidence (above).
- `preconditions` are **unverified assumptions** unless `preconditions_confirmed`
  is true; the report labels them.
- `safety_boundaries` are passed to the actor as an explicit **task constraint**
  (appended to `spec.task`), so the actor can respect them. The runtime safety
  layer does **not** read or auto-enforce Case-specific boundaries — it stays the
  independent `safety_mode` gate.

## Commands

From the repo root, prefer `.venv/bin/python`:

```bash
S=.agents/skills/phone-agent-live-diagnosis/scripts

# detached real-device run: persists case+spec, returns run_id/run_dir/pid now
.venv/bin/python $S/run_diagnosis.py start path/to/case.json --device-id <serial>
# wait for a real run_end, then analyze + report
.venv/bin/python $S/run_diagnosis.py wait <run_dir>
# foreground convenience: start + wait + finalize
.venv/bin/python $S/run_diagnosis.py case path/to/case.json --device-id <serial>
# ad-hoc target (no checkpoints; Case acceptance will be unknown)
.venv/bin/python $S/run_diagnosis.py run "打开设置并进入 Wi-Fi"
# offline synthetic smoke (unique new dir; no device/model/network/config)
.venv/bin/python $S/run_diagnosis.py dry-run

# monitor a detached run / answer the current pending HITL / request stop
.venv/bin/python $S/run_diagnosis.py monitor <run_dir> --follow
.venv/bin/python $S/run_diagnosis.py hitl <run_dir> "确认继续"
.venv/bin/python $S/run_diagnosis.py stop <run_dir>
# re-derive summary.json, or render-only from a saved summary (no re-analysis)
.venv/bin/python $S/run_diagnosis.py analyze <run_dir>
.venv/bin/python $S/run_diagnosis.py report  <run_dir>
.venv/bin/python $S/run_diagnosis.py status  <run_dir>
```

`start` is explicit and non-blocking: it does not finalize. `wait`/`analyze`
produce `summary.json` + `report.html`. `report` is render-only and never
mutates the saved summary. `hitl` refuses an empty answer, a non-existent run, a
terminal run, a dead process, a run that already has a stop request, and a second
answer while a previous one is submitted-but-not-yet-consumed. `monitor` and
`status` re-derive state live from the IPC files (shared derivation), so an ended
run never keeps reporting `running`.

Exit codes for `case`/`run`/`wait` follow the harness, not the worker process:
`0` succeeded, `2` ended non-success, `3` timeout (still running), `4` process
died without a `run_end`, `5` still running. The runner process exit code is
**never** used as success.

## Configuration

On the **real launch path** `run_diagnosis.py` calls the existing
`phone_agent.v2.config.load_project_env()` and then `V2Config.from_env`, so
precedence is CLI flag > shell env (`PHONE_AGENT_*`) > project `.env` > default,
plus a single `models.json` layer and optional `roles` (see
`pages/configuration.md`). Offline commands (`dry-run`, `analyze`, `report`,
`monitor`, `status`) never read local config. Plugin authorization is the
runner's normal path (`taskwizard.capabilities` entry points / manifest).

Safety defaults to `wary`: a risky execution call is **not executed and no human
is summoned** — the model gets a warning (world fact + options) and must resend
with `confirm_irreversible=true`. `ask_user` / `take_over` always interrupt.

## Evidence contract (the run folder)

```text
<output_dir>/<run_id>/
  spec.json                 # RunSpec (resolved config + fingerprint snapshot)
  events.jsonl              # runner IPC event stream (state authority)
  control.jsonl             # stop / hitl channel (submitted, not consumed)
  run.json                  # recorded summary (NOT a run_end event)
  <run_id>.evidence.jsonl   # producer diagnostic stream (replay authority)
  evidence.jsonl            # derived stable copy of the producer stream
  screenshots/screen-<n>.png  # decoded frames; reference frames use ref ids
  traces/<run_id>.jsonl     # P0 #6 production trace (64-char, base64-free)
  case.json summary.json report.html status.json
```

The producer `<run_id>.evidence.jsonl` is authoritative; `evidence.jsonl` is only
a derived copy and never shadows a growing producer file. `report.html` is
**offline and self-contained**: no CDN, no remote fonts, no external scripts. It
only emits relative local paths; screenshot sources must match
`screenshots/screen-<id>.png`. The run dir is owner-private (`0700`) and
artifacts are `0600`.

## Gotchas

- **Don't diagnose against v1.** No `reflect`/`acceptance` node, no
  `GoalContract`, no `evals/run_eval.py`.
- **Visible text, not hidden thoughts.** The replay's `model_text` is the
  assistant message the provider actually returned; hidden reasoning is never
  recorded.
- **Reference frames.** A failed observation may still return its last valid
  frame as an *unverified reference* (no `screen_seq`); it is labelled 未验证 and
  never counted as a fresh observation or acceptance evidence.
- **Verifier status is `unknown` without an authoritative audit.** A finish
  receipt does not prove the verifier ran/passed; fail-open outage is `skipped`,
  never displayed as `pass`.
- **Budget vs ledger.** `visible_used_tokens` is only the actor's provider-
  reported usage; the harness `UsageLedger` (aux + estimates) is not exported, so
  `ledger_used_tokens` is `unknown`.
- **`dry-run` is synthetic and isolated.** It always uses the built-in synthetic
  Case and a unique new directory; it never deletes existing data or reads local
  config / global memory.
- **Bad JSONL is reported, not hidden.** Torn/invalid lines are skipped but
  surfaced as bounded `data_issues` in the summary and report.
- **Don't auto-edit business code** unless the user separately asks for a fix.

## Entry points & compatibility

`.agents/skills/phone-agent-live-diagnosis` is the **single canonical source**.
Because some agent front ends may not read `.agents/skills`, the repo keeps thin
compat entry points:

- `.claude/skills/phone-agent-live-diagnosis`
- `.codebuddy/skills/phone-agent-live-diagnosis`

Both are **relative symlinks** to `../../.agents/skills/phone-agent-live-diagnosis`
(not copies), so they can never go stale. They are verified offline by
`tests/skill/test_entrypoints.py` (link exists, resolves to canonical, content
bytes match, and not gitignored). This is what we can validate locally; it does
not assert that any particular CLI auto-discovers the skill from these paths.

## References (load on demand)

- `references/README.md` — index and reading order.
- `references/case-format.md` — Case JSON schema + a synthetic example.
- `references/run-and-monitor.md` — runner IPC, monitor/HITL/stop, permissions.
- `references/evidence-contract.md` — event schemas + report artifacts.
- `references/source-map.md` — category → candidate v2 source files.
- `references/offline-smoke.md` — the offline smoke and how to run it.
