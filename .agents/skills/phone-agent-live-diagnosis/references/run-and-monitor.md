# Run, monitor, HITL, stop

The skill never touches the device directly. A real run is launched as a
subprocess through the formal runner, using the same config precedence, safety
policy, model/role resolution and plugin authorization as the web console.

## Launch

`run_diagnosis.py start <case.json> [flags]` (or `run "<target>"`):

1. loads the project ``.env`` via the existing
   ``phone_agent.v2.config.load_project_env()`` and then resolves
   ``V2Config.from_env(overrides)`` (CLI > shell env > `.env` > default);
2. builds a `RunSpec` with `phone_agent.v2.run_ipc` helpers
   (`resolved_config_dict`, `config_fingerprint`, `app_kb_generation`,
   `capability_snapshot`, `write_run_spec`);
3. enables the diagnostic evidence stream **only via spec overrides**
   (`diagnostic_evidence`, `diagnostic_evidence_dir`, `diagnostic_unredacted`) —
   the production default stays off;
4. persists `case.json` + `spec.json` **before** spawning
   `python -m phone_agent.runner <spec.json>` **detached**, then returns
   immediately with `run_id` / `run_dir` / `pid` / initial state.

`start` does not wait or finalize. Use `wait <run_dir>` (poll to a real
`run_end`, then analyze + report) or `analyze`/`report` later. `case`/`run` are
the foreground convenience (`start` + `wait` + finalize) and accept `--timeout`;
they flush the launch info (run_id/run_dir/pid) immediately so another terminal
can answer HITL. The runtime owns ``runner.pid`` (written at start, removed on
terminal); the launcher never writes it, and a dead process without a `run_end`
is detected so `wait` does not spin for the full timeout.

Exit codes for `case`/`run`/`wait`: `0` harness succeeded, `2` ended
non-success, `3` timeout (still running), `4` process died without `run_end`,
`5` still running. The runner's **process exit code is never used as harness or
Case success**. If spawn fails, `case.json`/`spec.json` are preserved and
`status.json` records the error.

The runner writes `events.jsonl`, `control.jsonl`, `run.json` and (with the
diagnostic override) `<run_id>.evidence.jsonl` + `screenshots/`. The
`<run_id>.evidence.jsonl` producer stream is authoritative; `evidence.jsonl` is
a derived copy. An explicit evidence file passed to `analyze` is used exactly;
if several producers exist and none is named, analysis fails closed as ambiguous.

## Monitor

`run_diagnosis.py monitor <run_dir> [--follow]` prints one JSON line derived
from real state:

```json
{"run_id": "...", "process_alive": true, "pid": 123, "stop_requested": false,
 "run_end_seen": false, "run_summary_present": false, "harness_state": "running",
 "finished": false, "steps": 7, "unresolved_hitl": [],
 "hitl_submitted": 0, "hitl_consumed": 0, "last_event": "tool_result"}
```

`process_alive` comes from `runner.pid` via `pid_is_alive`; `harness_state` comes
from the IPC `run_end` **event** (a `run.json` alone is not terminal and is
reported via `run_summary_present`). `steps` is the highest step seen on events
while running. `stop_requested` is independent of the terminal state.

## Human interaction (HITL)

When the runtime raises a HITL interrupt the runner emits `pending_hitl` with the
prompt and blocks on the control channel. The coding agent shows the prompt to
the human, then writes the human's answer:

```bash
.venv/bin/python $S/run_diagnosis.py hitl <run_dir> "确认继续"
```

This appends `{"type": "hitl", "answer": "..."}` to `control.jsonl`. A control
answer only records that a human **submitted** a reply; only the runner's
`pending_hitl: null` clearing event proves it was **consumed**. `hitl` refuses:

- an empty answer;
- a non-existent run dir;
- a run that already has a `run_end` (terminal);
- a second answer while a previous one is submitted-but-not-yet-consumed;
- any answer when there is no currently pending prompt (no pre-queuing).

**The skill never auto-approves** — an unanswered prompt stays unresolved in the
report.

In `wary` mode most risky calls do not interrupt at all: they return a warning
and the model must resend with `confirm_irreversible=true`. In `hard` mode a
gated actuation call interrupts with `approve` / `reject`. `ask_user` /
`take_over` always interrupt.

## Stop

```bash
.venv/bin/python $S/run_diagnosis.py stop <run_dir>
```

Appends `{"type": "stop"}`. The runner soft-stops at the next model boundary and
sets a takeover reason. **A stop request is not an ended run**: only a real
`run_end` event is terminal. If the process dies first, `monitor` reports
`stop_requested=true` with `run_end_seen=false`. `stop` on an already-terminal
run is refused.

## Permissions & escalation

Real runs need host ADB (and possibly Metal/network for grounding providers). If
a command fails with sandbox-shaped errors (`Operation not permitted`, blocked
device access, DNS/registry failures), rerun that specific command through the
agent front end's escalation with a short user-facing reason. Never relax the
sandbox for destructive commands. `dry-run` needs no escalation.
