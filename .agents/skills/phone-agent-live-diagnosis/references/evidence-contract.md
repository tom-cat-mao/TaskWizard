# Evidence contract

Two append-only planes plus a terminal summary. The analyze layer reads all of
them; each is fail-open (a torn final line is skipped).

## Runner IPC — `events.jsonl` (state authority)

Emitted by `phone_agent.v2.run_events.WebEventMiddleware`. Events observed by the
analysis:

| event | meaning |
|---|---|
| `model_request` | phase `start`; carries `requested_model` (bound model label) |
| `model_call` | per call: `latency_ms`, `tokens`, `requested_model`, `actual_model` (provider-reported only), `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_write_tokens` (each may be `null`), `error` |
| `tool_call` / `tool_result` | tool name + redacted args, then result text + `ok` + latency |
| `screen` | a rendered frame reference; `reference=true` when it is an unverified reference frame |
| `taskdoc_snapshot` | the board text whenever it changes |
| `pending_hitl` | a prompt; followed by a `prompt: null` event once answered |
| `safety_warning` | a `wary` warning was returned in place of execution |
| `stopping` | the soft-stop jump fired |
| `capability_snapshot` / `memory_generation_drift` | assembly facts |
| `run_end` | terminal `status` + `result{success, reason, steps}` + `tokens_total` |

`control.jsonl` carries `{"type":"stop"}` / `{"type":"hitl","answer":...}`. A
control answer only records a **submitted** reply; the runner's
`pending_hitl: null` event is the proof it was **consumed** (tracked separately).
`run.json` is a recorded summary artifact — it is shown separately and is **not**
a `run_end` event. `spec.json` carries the resolved overrides (effective config).

## Diagnostic stream — `<run_id>.evidence.jsonl` (replay authority)

The producer stream. `evidence.jsonl` is a derived stable copy and must never
shadow the growing producer file.

Emitted by `phone_agent.v2.middleware.diagnostic.DiagnosticEvidenceWriter` when
the spec enables it. Full-fidelity by default (local, owner-private):

| event | meaning |
|---|---|
| `run_start` | run id, goal, config digest |
| `model_request` | per step: message/image counts, pruned-screen count, TaskDoc presence, context chars |
| `model_response` | `thinking` = the assistant **visible text** the provider returned (never hidden reasoning), tool calls, raw `usage` |
| `taskdoc_snapshot` | goal / amendments / items (+ `evidence_note`) / facts / open count |
| `tool_invoke` / `tool_observation` | args in, then latency + full result text + parsed `[OBS]` + `image{present,screen_seq,bytes,path,reference}` |
| `run_end` | terminal state |

Screenshots are decoded to `screenshots/screen-<seq>.png` (a reference frame uses
its own ref id). The JSONL never carries base64. Tool `intent` / `note` are in
the model tool-call args and in the per-call receipts, and the report surfaces
them — but acceptance does **not** treat them as evidence.

Malformed / torn JSONL lines are skipped yet reported as bounded `data_issues`
(``{source, line, reason}``); the report surfaces them instead of silently
claiming completeness.

## Terminal mapping (`events.RunnerEventsView.harness_terminal`)

Only the `run_end` **event** is terminal. A `run.json` without a `run_end`
event yields `unknown_terminated` (`run_end_seen=false`,
`run_summary_present=true`), never success.

| state | source |
|---|---|
| `succeeded` | `run_end` `result.success` true (finish accepted) |
| `takeover` | `run_end` status `takeover` (not a console stop) |
| `stopped` | takeover reason == `用户从 Web 控制台停止` |
| `token_budget_exhausted` | status / reason |
| `loop_fuse` | status / reason |
| `error` / `failed` | status `error` / `failed` |
| `running` / `stopping` | no `run_end`; stop requested or not |
| `unknown_terminated` | no `run_end` but a `run.json` summary exists |

Usage totals report `null` for any missing field. Completeness is judged
**per call** (not by unioning fields across calls): ``total_tokens`` exists only
when every call reported both input and output; a partially-reported cache stays
``None`` even if other calls reported a cache value. A ``coverage`` block reports
how many calls carried each field.

``status`` / ``monitor`` re-derive the above live from the IPC files (shared
derivation): a dead process without a ``run_end`` becomes ``unknown_terminated``;
the persisted ``status.json`` is only a derived cache (``status_source`` says
``live_events`` or ``derived_cache``).

## `summary.json` blocks

`run_id, created_at, target, verdict, command, duration_sec, steps` then:

- `harness_terminal` — state / finished / finish_summary / takeover_reason /
  reason / stop_requested / run_end_seen / run_summary_present / steps;
- `run_summary` — the recorded `run.json` summary, shown separately;
- `data_issues[]` — bounded JSONL parse issues;
- `case` + `case_acceptance` — Case definition and per-checkpoint status;
- `diagnosis` — inference caveat;
- `finish_gate` + `finish_verifier` — two-step receipts + verifier status
  (`unknown` without an authoritative audit; `fail` only from an in-band
  rejection or a persisted verdict; fail-open outage is `skipped`, never `pass`);
- `taskdoc_final`, `context`, `context_errors`, `hitl` (submitted/consumed/
  unresolved), `safety`, `budget` (visible usage vs `unknown` ledger),
  `tool_health`, `grounding`, `visual`, `windowing`, `model`, `fallback`,
  `resolver`, `memory`, `capabilities`, `replay[]`, `findings[]`,
  `recommendations[]`.

Re-analysis preserves previously saved `created_at` / `duration_sec` / `command`
and notes; it does not wipe manual metadata.

## `report.html`

Offline, self-contained, Chinese. Tabs: 三层裁定 / Case 验收 / 逐步回放 / 问题与
安全 / 性能与上下文 / 源码归因·推断 / 原始文件. It never emits a CDN URL or a
remote/absolute asset; screenshot `src` must match
`screenshots/screen-<id>.png`. The run dir is `0700`, artifacts `0600`.
