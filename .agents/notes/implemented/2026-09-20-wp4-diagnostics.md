# Agent Note: Diagnostic observation augmentation (WP4)

Status: implemented

## Problem

A real-device run复盘 exposed three observability gaps:

1. **Accessibility dump failure details missing**: When marks() fails, the evidence only shows `marks (0) [accessibility:provider_error]`. The exception type (`TimeoutError` vs Android's internal errors) never landed in trace/evidence, making it impossible to distinguish "our 3s timeout killed process" from "Android 10s idle failed".

2. **parse_summary lifecycle limited to header rendering**: The parser's `parse_summary` (total_candidates, per-window counts, filter tallies) lives only in-process; the only consumer is the observation header renderer (tools/_obs.py:109-121). Trace/evidence never persisted this data, losing valuable diagnostics for post-run analysis.

3. **Skill analyzer lacks automated discovery rules**: `.agents/skills/phone-agent-live-diagnosis/scripts/analyze.py` has no built-in detection for "window quota starvation" or "accessibility dump failures", forcing manual grep of steps instead of auto-generated findings.

## Decision

**WP4 adds three layers of diagnostic enrichment without breaking P0 #6**:

### ① Exception type + parse_summary to tool_observation events

In `phone_agent/v2/middleware/diagnostic.py::_emit_tool_observation`:
- Extract `accessibility_error` by regex-parsing `[accessibility:<code>]` annotations from OBS text when mark_count == 0
- Pull `parse_summary` from `session.obs.parse_summary` and record in JSONL event under same event schema as other middleware fields
- Apply P0 #6 redaction rules unchanged: text truncated to 64 chars, sensitive substrings replaced with `<redacted>`, base64 never logged

```python
self._write({
    "event": "tool_observation",
    ...,
    "accessibility_error": self._text(accessibility_error) if accessibility_error else None,
    "parse_summary": parse_summary_data,
    ...
})
```

### ② Raw XML evidence directory prepared for future use

Add `_write_xml_evidence()` method that:
- Only executes when `diagnostic_mode=True` (enabled AND unredacted), i.e., live-diagnosis skill runs
- Enforces size cap ≤ 256KB per dump with truncation marker
- Writes to `<evidence_dir>/xml_evidence/<run_id>-<ts>.xml` with 0o600 permissions
- Records relative path in `original_xml_path` field for replay
- Production mode (diagnostic_mode=False) has **zero cost**—this branch never executed

Note: raw_xml attr does NOT currently exist on session.obs; field is placeholder for future enhancement when device_factory exposes full XML dumps.

### ③ Skill analyze.py two new findings

In `scripts/analyze.py::build_windowing`:
- Track `accessibility_failure_count` across all tool_calls; when ≥ 2 occurrences → add to `accessibility_failures` list
- Check window quota starvation: foreground app marks < 20% of retained total AND shell_windows source has input/system overlays → add to `starvation_events` list

In `scripts/analyze.py::build_findings`:
- New category `window_quota_starvation` (P2 severity): fires when starvation_events count > 0, examples show step/mark ratio
- New category `accessibility_dump_failures` (P1 severity): fires when accessibility_failure_count >= 2, suggestions include device config checks

Findings render in HTML report diagnosis section with source-file links and verification steps.

## Alternatives considered

**Alternative A: Add raw_xml to accessibility.py directly**
- **Strongest argument**: Immediate capture without dependency on session.obs contract changes
- **Why rejected**: Accessibility layer forbidden (WP4 scope boundary); also raw XML often unnecessary noise in prod, should be opt-in only

**Alternative B: Always-on XML capture with selective redaction**
- **Strongest argument**: Full fidelity always available for debugging
- **Why rejected**: Storage cost grows linearly with run length; violates "production zero-cost" principle; redaction can't compress binary blob anyway

**Alternative C: Parse_summary via environment variable flag**
- **Strongest argument**: Opt-in saves token budget for users who don't need it
- **Why rejected**: parse_summary is lightweight (<1KB per observation), always beneficial for live-diagnosis; simpler to just log unconditionally

**Alternative D: Post-hoc reconstruction of parse_summary from XML**
- **Strongest argument**: Don't duplicate work; call parser once at end
- **Why rejected**: Parser not designed for replay; runtime parsing already done; storage is cheaper than re-computation

## Consequences

**Benefits**:
- Accessibility timeout causes now distinguishable from Android provider errors
- Windowed marks parsing statistics available for quota饥饿 diagnosis
- Live-diagnosis skill can auto-generate findings for common failure patterns
- Evidence stream richer without changing existing observers

**Trade-offs**:
- evidence.jsonl grows ~50-200 bytes per observation (parse_summary dict)
- Diagnostic writer code complexity increases (~70 lines added)
- raw_xml field currently unused (TODO placeholder), may confuse readers
- Starvation threshold (20%) heuristic; may need tuning per-device

**Future work**:
- Add raw_xml extraction when accessibility layer refactored to expose it
- Tune starvation percentage empirically across Android versions
- Consider caching parse_summary delta encoding (only changed fields)
- Expose diagnostic_mode via PHONE_AGENT_DIAG_EVIDENCE env var (currently tied to enable+unredacted)

**P0 compliance preserved**:
- No base64 screenshots in evidence.jsonl (always split to metadata)
- Text still truncated/redacted unless unredacted=True
- Sensitive data (payment passwords, tokens) still redacted per _redact module
