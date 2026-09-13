# Case format

A **Case** is the coding agent's executable test intent. It is plain JSON and is
validated by `scripts/case.py::Case.from_dict`.

```json
{
  "id": "settings-wifi",
  "title": "打开设置并进入 Wi-Fi 页面",
  "goal": "打开系统设置并进入 Wi-Fi 页面，确认页面若干条目可见",
  "preconditions": ["设备已解锁并停在主屏", "系统语言为中文或英文"],
  "acceptance": [
    {"id": "A1", "description": "观测记录的前台应用为 com.android.settings", "match": ["[OBS] app=com.android.settings "]},
    {"id": "A2", "description": "人工对照末帧截图确认已进入 Wi-Fi 页面，而非仅看到设置页的 Wi-Fi 入口"}
  ],
  "safety_boundaries": ["不得点击恢复出厂设置/清空数据", "不得修改或忘记已连接网络"],
  "preconditions_confirmed": false,
  "notes": "可选备注"
}
```

Fields:

- `id` / `title` / `goal` — `goal` is required and is the text handed to the
  agent. `id` defaults to a slug of the title/goal.
- `preconditions[]` — world facts assumed before the run. They are **unverified
  assumptions** unless `preconditions_confirmed` is `true`; the report labels
  them. A violation is a harness setup problem, not an actor failure.
- `acceptance[]` — checkpoints evaluated **only** against objective device
  observations. The objective corpus is the real ``[OBS] app=…`` segment of a
  tool return; a success prefix that merely echoes the target name is candidate
  only, as are failed receipts and unverified reference frames (``image.reference``
  or no committed ``screen_seq``):
  - `match[]`: literals searched in the objective observation corpus;
  - `contradict[]`: literals that, without an objective match, are positive
    counter-evidence;
  - a checkpoint with neither, or with no objective evidence, stays `unknown`.
  - if an objective match and an objective contradiction both appear, the
    checkpoint is `unknown` (a conflict never resolves to `pass`).
  - TaskDoc evidence notes / facts and the actor's `finish` self-claim are
    **candidate** evidence: they can be shown but never auto-pass. The goal,
    per-step `intent`/`note`/`target_description`, failure receipts, and
    unverified reference frames are not acceptance evidence at all.
- `safety_boundaries[]` — passed to the actor as an explicit task constraint
  (they are appended to `spec.task`), so the actor can respect them. The runtime
  safety layer does **not** read or auto-enforce Case-specific boundaries.

Literal matches establish only the stated text checkpoint, not general semantic task
completion. For example, a settings list can contain “Wi-Fi” without the agent
having entered the Wi-Fi page. Do not use that word alone to auto-pass navigation.
Leave ambiguous or visual checks without a matcher (`unknown`) and report the
human/coding-agent review separately with its screenshot/step references. The
example's A2 deliberately requires this review.

Rollup: any `unmet` → `fail`; all `met` → `pass`; otherwise not-finished →
`incomplete`; otherwise → `unknown`. A finished harness never promotes `unknown`
to `pass`.

The objective corpus for matching is built by
`analyze.build_acceptance_evidence` from `[OBS]` lines and successful
perception/locate returns. Candidate entries come from TaskDoc evidence notes /
facts and the actor's finish self-claim. See also
`cases/settings-wifi.synthetic.json`.
