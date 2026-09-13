# Source map — candidate owners per report category

The report maps a fired category to the v2 file(s) that **own** the behavior, so
a reviewer knows where to look first. `path:line` anchors mark where a symbol
lives, not where a bug was proven. Treat every mapping as a candidate; confirm it
against the cited step evidence before calling anything a root cause.

The machine-readable table is `scripts/sourcemap.py::V2_SOURCE_RULES`; the
contract test `tests/skill/test_sourcemap.py` keeps it honest.

| Category | Layer | Candidate v2 files |
|---|---|---|
| `grounding_addressing` | grounding | `v2/tools/actuation.py`, `v2/tools/perception.py`, `v2/resolver.py`, `v2/session.py` |
| `actuation_arg` | actuation | `v2/tools/actuation.py`, `v2/coords.py` |
| `launch` | launch | `v2/tools/actuation.py`, `v2/names.py`, `config/apps.py`, `config/policy.py` |
| `resolver` | resolver | `v2/names.py`, `v2/tools/actuation.py`, `v2/middleware/trace.py` |
| `deliverable` | deliverable | `v2/tools/deliverable.py`, `v2/capabilities.py` |
| `finish_gate` | finish | `v2/tools/control.py`, `v2/review.py`, `v2/taskdoc.py` |
| `finish_verifier` | finish | `v2/verify.py`, `v2/tools/control.py` |
| `safety` | safety | `v2/middleware/safety.py`, `config/policy.py` |
| `taskdoc` | taskdoc | `v2/tools/taskdoc.py`, `v2/taskdoc.py` |
| `hitl` | safety | `v2/tools/control.py`, `v2/middleware/safety.py` |
| `observation` | observation | `v2/tools/_obs.py`, `v2/session.py`, `adb/screenshot.py` |
| `secure_screenshot` | observation | `v2/tools/_obs.py`, `v2/session.py`, `adb/screenshot.py` |
| `context` | context | `v2/middleware/images.py`, `v2/middleware/taskdoc.py` |
| `visual` | visual | `v2/tools/_obs.py`, `v2/tools/actuation.py`, `v2/middleware/images.py` |
| `model` | model | `v2/model.py`, `v2/agent.py` |
| `recall` | memory | `v2/recall.py` |
| `capabilities` | assembly | `v2/capabilities.py` |

## Architecture note — there is no graph

The v1 LangGraph node model is deleted. Never map a finding to `goal` / `plan` /
`execute` / `reflect` / `acceptance` nodes, to `GoalContract`, or to
`evals/run_eval.py`. Map it to a tool, the finish gate/verifier, the TaskDoc
board, or a middleware listener.

## Runtime facts the map assumes

- Safety default is `wary` (warning flow; `v2/middleware/safety.py`).
- Image hygiene keeps the newest `PHONE_AGENT_IMAGE_KEEP` (default 2) image
  messages (`v2/middleware/images.py`).
- `finish` is two-step with an independent verifier; verifier failure is
  fail-open `skipped` (`v2/tools/control.py`, `v2/verify.py`).
- The token budget (cost) and `max_model_calls` (loop fuse) are independent
  (`v2/middleware/budget.py`).
- The stagnation nudge was removed; the flow line is transcript-derived
  (`v2/middleware/taskdoc.py`).
