# References — index and reading order

The skill's operational contract is `../SKILL.md`. Read it first. Load the files
below only when the task needs their detail.

| When you need… | Read |
|---|---|
| The Case JSON schema + a worked synthetic example | `case-format.md` |
| How a run is launched, monitored, answered, stopped | `run-and-monitor.md` |
| The exact event schemas and report artifacts | `evidence-contract.md` |
| Which v2 source files own a report category | `source-map.md` |
| The offline smoke and what it proves | `offline-smoke.md` |
| A ready-to-run synthetic Case | `cases/settings-wifi.synthetic.json` |

Design invariants repeated for emphasis:

- State comes from **real events** (`events.jsonl` / `run.json` / `control.jsonl`),
  never from intent. A requested stop is not an ended run.
- `finished` is a harness fact; Case acceptance is a separate, evidence-only
  judgment; source findings are inference, not proven root cause.
- The report is offline: no CDN, no remote assets, local relative paths only.
