"""Render ``summary.json`` (+ evidence) into a self-contained offline HTML report.

The reader is the device owner / reviewing coding agent on the same machine, so
the report is **fully offline**:

* **No CDN, no remote fonts, no external assets.** Everything (CSS, JS, data) is
  inline; there is no ``@import url(...)`` and no ``<script src>``.
* **No remote navigation.** The renderer only ever emits an ``href``/``src`` for
  a relative local path with no scheme and no ``..``; screenshot sources must
  match ``screenshots/screen-<id>.png`` exactly. Anything else is dropped.
* **Escaped data.** All strings cross ``esc()`` before insertion into the DOM;
  the JSON island neutralizes ``</`` so the payload can never close the script.
* **Private by construction.** The report is written ``0600`` inside the private
  run dir (the driver locks it down) and shows a privacy banner.

The three judgments — harness terminal, Case acceptance, diagnostic inference —
are rendered as distinct sections and never merged.
"""

from __future__ import annotations

import json
from typing import Any


def _escape_report_data(payload: str) -> str:
    """Escape a JSON payload for safe embedding in a ``<script>`` island."""

    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("</", "<\\/")
    )


def sanitize_summary_paths(summary: dict[str, Any]) -> dict[str, Any]:
    """Strip remote / absolute / traversing values from known path fields.

    Only path-like keys are touched; other strings (tool results, model text,
    targets) are left verbatim so the sanitizer never corrupts evidence content.
    Screenshot ``path`` values are restricted to the middleware's own naming.
    """

    import copy
    import re

    shot_re = re.compile(r"^screenshots/screen-[A-Za-z0-9_.-]+\.png$")
    path_keys = {"summary", "report", "evidence", "run_dir", "trace", "evidence_stream"}

    def _is_unsafe_path(value: str) -> bool:
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) or value.startswith("//"):
            return True
        if value.startswith("/") or value.startswith("~"):
            return True
        return ".." in value.replace("\\", "/")

    def _clean(value: Any, key: str | None = None) -> Any:
        if isinstance(value, str):
            if key == "path":
                return value if shot_re.match(value) else None
            if key in path_keys:
                return None if _is_unsafe_path(value) else value
            return value
        if isinstance(value, dict):
            return {str(k): _clean(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [_clean(item, key) for item in value]
        return value

    return _clean(copy.deepcopy(summary))


def render_html(
    summary: dict[str, Any], evidence: list[dict[str, Any]] | None = None
) -> str:
    """Render ``summary`` (+ optional evidence stream) to an HTML string."""

    safe_summary = sanitize_summary_paths(summary)
    payload = json.dumps(
        {"summary": safe_summary, "evidence": evidence or []}, ensure_ascii=False
    )
    return HTML_TEMPLATE.replace("__REPORT_DATA__", _escape_report_data(payload))


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <base target="_blank">
  <title>Phone Agent 实机诊断（离线）</title>
  <style>
    :root {
      --primary: #1E40AF; --primary-soft: #DBEAFE; --accent: #F59E0B;
      --bg: #F1F5F9; --panel: #FFFFFF; --ink: #0F172A; --muted: #64748B;
      --line: #E2E8F0; --success: #15803D; --failed: #B91C1C;
      --blocked: #B45309; --radius: 10px;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--ink);
      font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
    .mono, code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; }
    header { background: linear-gradient(135deg, #0F172A 0%, #1E293B 60%, #1E3A8A 100%);
      color: white; padding: 26px 32px 22px; border-bottom: 4px solid var(--accent); }
    .header-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; max-width: 1400px; }
    h1 { margin: 0 0 10px; font-size: 24px; line-height: 1.2; }
    h2 { margin: 0 0 12px; font-size: 17px; display: flex; align-items: center; gap: 8px; }
    h2::before { content: ""; width: 4px; height: 16px; border-radius: 2px; background: var(--accent); }
    h3 { margin: 14px 0 8px; font-size: 14px; }
    .wrap { word-break: break-all; overflow-wrap: break-word; }
    .subtitle { color: #CBD5E1; max-width: 1000px; font-size: 14px; line-height: 1.5; }
    .verdict-chip { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; border-radius: 999px;
      font: 700 14px ui-monospace, monospace; white-space: nowrap; background: rgba(255,255,255,.12); border: 1px solid rgba(255,255,255,.25); }
    .verdict-chip.success { background: rgba(21,128,61,.35); border-color: #4ADE80; color: #BBF7D0; }
    .verdict-chip.takeover, .verdict-chip.budget_exhausted, .verdict-chip.loop_fuse { background: rgba(180,83,9,.4); border-color: #FCD34D; color: #FDE68A; }
    .verdict-chip.stopped { background: rgba(100,116,139,.4); border-color: #CBD5E1; color: #E2E8F0; }
    .verdict-chip.error, .verdict-chip.failed { background: rgba(185,28,28,.35); border-color: #F87171; color: #FECACA; }
    .verdict-chip.uncertain { background: rgba(100,116,139,.35); border-color: #CBD5E1; color: #E2E8F0; }
    .shell { padding: 20px 32px 40px; max-width: 1400px; }
    .privacy { background: #0F172A; color: #FDE68A; border: 1px solid #B45309; border-radius: 8px;
      padding: 8px 12px; font-size: 12.5px; margin-bottom: 14px; }
    .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }
    .card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
      padding: 14px 16px; box-shadow: 0 1px 3px rgba(15, 23, 42, .06); }
    .kpi-label { color: var(--muted); font-size: 12px; margin-bottom: 6px; text-transform: uppercase; letter-spacing: .04em; }
    .kpi-value { font-size: 18px; font-weight: 700; }
    .badge { display: inline-flex; align-items: center; min-height: 22px; border-radius: 999px; padding: 2px 9px;
      font: 600 12px ui-monospace, monospace; background: #E0E7FF; color: var(--primary); border: 1px solid #BFDBFE; }
    .badge.success, .badge.met, .badge.pass { background: #DCFCE7; color: var(--success); border-color: #86EFAC; }
    .badge.failed, .badge.error, .badge.unmet, .badge.fail { background: #FEE2E2; color: var(--failed); border-color: #FCA5A5; }
    .badge.blocked { background: #FEF3C7; color: var(--blocked); border-color: #FCD34D; }
    .badge.unknown, .badge.incomplete { background: #F1F5F9; color: var(--muted); border-color: #CBD5E1; }
    .alert { border-radius: 8px; padding: 10px 14px; margin: 0 0 12px; font-weight: 700; word-break: break-all; overflow-wrap: break-word; }
    .alert.danger { background: #FEE2E2; color: var(--failed); border: 1px solid #FCA5A5; }
    .alert.warn { background: #FEF3C7; color: var(--blocked); border: 1px solid #FCD34D; }
    .alert.info { background: #DBEAFE; color: var(--primary); border: 1px solid #93C5FD; font-weight: 500; }
    .toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 14px 0; }
    button { border: 1px solid var(--line); background: white; color: var(--ink); border-radius: 8px;
      padding: 8px 12px; font: 500 13px system-ui; cursor: pointer; transition: all .15s; }
    button:hover { border-color: var(--primary); color: var(--primary); }
    button.active { background: var(--primary); border-color: var(--primary); color: white; }
    .tab { display: none; } .tab.active { display: block; animation: fadeIn .18s ease; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
    .grid-2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid var(--line); padding: 9px 10px; vertical-align: top; text-align: left; }
    th { color: #334155; background: #F8FAFC; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
    tr:last-child td { border-bottom: none; }
    .cause-card { background: var(--panel); border: 1px solid var(--line); border-left: 5px solid var(--muted);
      border-radius: var(--radius); padding: 16px 18px; margin-bottom: 12px; }
    .cause-card.sev-P0 { border-left-color: var(--failed); } .cause-card.sev-P1 { border-left-color: var(--accent); }
    .cause-card.sev-P2 { border-left-color: var(--primary); } .cause-card.sev-Info { border-left-color: var(--success); }
    .cause-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
    .cause-what { font-size: 15px; font-weight: 700; line-height: 1.45; margin: 6px 0 10px; }
    .cause-block { display: grid; grid-template-columns: 72px minmax(0,1fr); gap: 6px 12px; font-size: 13.5px; line-height: 1.6; }
    .cause-label { color: var(--muted); font-weight: 600; white-space: nowrap; }
    .cause-src { margin-top: 10px; padding: 8px 10px; background: #F8FAFC; border: 1px dashed var(--line); border-radius: 6px; font-size: 12.5px; }
    .step { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); margin-bottom: 16px; overflow: hidden; }
    .step-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 12px 16px; background: #F8FAFC; border-bottom: 1px solid var(--line); }
    .step-no { font: 700 15px ui-monospace, monospace; color: var(--primary); }
    .step-body { display: grid; grid-template-columns: 300px minmax(0,1fr); gap: 16px; padding: 16px; }
    .shot-col { display: flex; flex-direction: column; gap: 6px; }
    .shot { display: block; width: 100%; border: 1px solid var(--line); border-radius: 8px; background: #0B1220; object-fit: contain; max-height: 520px; }
    .shot-missing { display: flex; align-items: center; justify-content: center; min-height: 160px; border: 1px dashed var(--line);
      border-radius: 8px; color: var(--muted); font-size: 12.5px; text-align: center; padding: 12px; }
    .shot-cap { font-size: 11.5px; color: var(--muted); text-align: center; }
    .think { background: #FFFBEB; border: 1px solid #FCD34D; border-radius: 8px; padding: 10px 12px;
      font-size: 13.5px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; margin-bottom: 12px; }
    .think.empty { background: #F8FAFC; border-color: var(--line); color: var(--muted); }
    .toolcall { border: 1px solid var(--line); border-radius: 8px; margin-bottom: 10px; overflow: hidden; }
    .toolcall.is-error { border-color: #FCA5A5; }
    .toolcall-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 8px 12px; background: #F8FAFC; }
    .toolcall-body { padding: 10px 12px; }
    .kv { font-size: 12.5px; color: var(--muted); margin-bottom: 4px; }
    details { margin-top: 8px; } summary { cursor: pointer; color: var(--primary); font-weight: 700; }
    pre { margin: 8px 0 0; padding: 12px; background: #0B1220; color: #E2E8F0; border-radius: 8px;
      overflow: auto; max-height: 360px; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
    .severity-P0 { color: #B91C1C; font-weight: 700; } .severity-P1 { color: #B45309; font-weight: 700; }
    .severity-P2 { color: #1E40AF; font-weight: 700; } .severity-Info { color: #15803D; font-weight: 700; }
    .muted { color: var(--muted); } .bar { height: 8px; border-radius: 4px; background: var(--primary-soft); overflow: hidden; }
    .bar > span { display: block; height: 100%; background: var(--primary); }
    .chip-cloud { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .dimension-stack { display: grid; gap: 14px; margin-top: 14px; }
    a { color: var(--primary); text-decoration: none; } a:hover { text-decoration: underline; }
    @media (max-width: 1100px) { .grid-2 { grid-template-columns: 1fr; } .step-body { grid-template-columns: 1fr; } }
    @media (max-width: 520px) { header, .shell { padding-left: 16px; padding-right: 16px; } }
  </style>
</head>
<body>
<script id="report-data" type="application/json">__REPORT_DATA__</script>
<header>
  <div class="header-row">
    <div>
      <h1>Phone Agent 实机诊断（离线）</h1>
      <div class="subtitle wrap" id="subtitle"></div>
    </div>
    <div id="verdictChip"></div>
  </div>
</header>
<main class="shell">
  <div class="privacy">本报告为本地私密产物（0600）：含未脱敏的设备画面引用与模型可见文本，请勿外发。截图以相对路径 <span class="mono">screenshots/</span> 引用；报告须与该目录同级。</div>
  <section class="kpis" id="kpis"></section>
  <nav class="toolbar" id="tabs"></nav>
  <section id="overview" class="tab active"></section>
  <section id="case" class="tab"></section>
  <section id="replay" class="tab"></section>
  <section id="problems" class="tab"></section>
  <section id="dimensions" class="tab"></section>
  <section id="source" class="tab"></section>
  <section id="raw" class="tab"></section>
</main>
<script>
const data = JSON.parse(document.getElementById('report-data').textContent);
const summary = data.summary || {};
const evidence = data.evidence || [];
const state = { tab: 'overview' };
const tabs = [
  ['overview', '三层裁定'],
  ['case', 'Case 验收'],
  ['replay', '逐步回放'],
  ['problems', '问题与安全'],
  ['dimensions', '性能与上下文'],
  ['source', '源码归因·推断'],
  ['raw', '原始文件'],
];
const VERDICT_LABEL = {
  success: 'harness 成功', takeover: '人工接管', stopped: '已停止（请求）',
  budget_exhausted: 'Token 预算耗尽', loop_fuse: '步数保险丝', error: '错误', uncertain: '不确定',
};
const CASE_LABEL = { pass: '通过', fail: '未通过', incomplete: '未完成', unknown: '未知' };
const STATUS_LABEL = { met: '有证据', unmet: '反证', unknown: '无证据' };
const CLASS_LABEL = {
  success: 'OK', observation: '观测', obs_capture_failed: '再观测失败',
  addressing_conflict: '寻址冲突', addressing_missing: '缺寻址', stale_mark: 'stale mark',
  ambiguous_resolve: '描述歧义', locate_no_match: '未定位', locate_provider_error: '定位失败',
  bad_coords: '坐标非法', bad_direction: '方向非法', ambiguous_app: 'app 歧义',
  launch_denied: '启动被拒', app_not_installed: '未安装', launch_failed: '启动失败', unknown_app: '未知 app',
  taskdoc_input_invalid: '任务板输入无效', taskdoc_validation_failed: '任务板校验失败', taskdoc_ok: '任务板已更新',
  finish_no_evidence: 'finish 无证据', finish_blocked_open_items: 'finish 被拦截',
  finish_review_packet: 'finish 复核包', finish_confirmed: 'finish 已确认', finish_ok: 'finish 通过',
  verifier_reject: '验收驳回', verifier_dispute_takeover: '验收反复驳回转接管',
  safety_warning: '安全预警', ask_user: '询问用户', takeover_requested: '请求接管', unknown: '未分类',
};
const ERROR_CLASSES = new Set([
  'obs_capture_failed','addressing_conflict','addressing_missing','stale_mark','ambiguous_resolve',
  'locate_no_match','locate_provider_error','bad_coords','bad_direction','ambiguous_app',
  'launch_denied','app_not_installed','launch_failed','unknown_app','taskdoc_input_invalid','taskdoc_validation_failed',
  'finish_no_evidence','finish_blocked_open_items',
]);
const SHOT_RE = /^screenshots\/screen-[A-Za-z0-9_.-]+\.png$/;
function safeShot(p) { return (typeof p === 'string' && SHOT_RE.test(p)) ? p : null; }
function safeLocalHref(p) {
  if (typeof p !== 'string' || !p) return null;
  if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(p) || p.startsWith('//')) return null;
  if (p.replace(/\\/g, '/').includes('..')) return null;
  return p;
}
function esc(v) { return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function json(v) { return esc(JSON.stringify(v, null, 2)); }
function badge(text, cls='') { return `<span class="badge ${esc(cls)}">${esc(text)}</span>`; }
function num(v) { return (v || v === 0) ? esc(v) : '—'; }
function row(k, v) { return `<tr><td class="mono">${esc(k)}</td><td class="wrap">${esc(v ?? '—')}</td></tr>`; }
function classBadge(cls) {
  const label = CLASS_LABEL[cls] || cls || '未分类';
  const ok = cls === 'success' || cls === 'finish_ok' || cls === 'finish_confirmed' || cls === 'taskdoc_ok' || cls === 'finish_review_packet';
  return badge(label, ERROR_CLASSES.has(cls) ? 'error' : (ok ? 'success' : ''));
}
function safeLink(label, p) {
  const href = safeLocalHref(p);
  return href ? `<a href="${esc(href)}">${esc(label)}</a>` : esc(label);
}

function render() {
  const m = summary.model || {};
  const usage = (m.token_usage || {}).total_tokens;
  document.getElementById('subtitle').innerHTML =
    `<strong>${esc(summary.target)}</strong><br><span class="mono wrap">${esc(summary.run_id)} · ${esc(summary.created_at || '')} · ${esc(summary.steps ?? '—')} 步 · ${esc(summary.duration_sec ?? 0)}s · token ${usage ? esc(usage) : '未上报'}</span>`;
  const v = summary.verdict || 'uncertain';
  document.getElementById('verdictChip').innerHTML =
    `<span class="verdict-chip ${esc(v)}">${esc(VERDICT_LABEL[v] || v)}</span>`;
  renderKpis(); renderTabs(); renderOverview(); renderCase(); renderReplay();
  renderProblems(); renderDimensions(); renderSource(); renderRaw();
}
function renderKpis() {
  const th = summary.tool_health || {};
  const v = summary.visual || {};
  const td = summary.taskdoc_final || {};
  const m = summary.model || {};
  const usage = m.token_usage || {};
  const ht = summary.harness_terminal || {};
  const ca = summary.case_acceptance || {};
  const cfg = summary.case || {};
  const items = [
    ['harness 终局', VERDICT_LABEL[summary.verdict] || summary.verdict || '—'],
    ['Case 验收', CASE_LABEL[ca.overall] || ca.overall || '—'],
    ['步数', num(summary.steps)],
    ['耗时', (summary.duration_sec ?? 0) + 's'],
    ['Token', usage.reported === false ? '未上报' : (usage.total_tokens ?? '—')],
    ['工具错误率', th.total_calls ? Math.round((th.error_rate || 0) * 100) + '%' : '—'],
    ['请求停止', ht.stop_requested ? '是（未决不是已结束）' : '否'],
    ['Case', cfg.id || '—'],
  ];
  document.getElementById('kpis').innerHTML = items.map(([k, val]) =>
    `<div class="card"><div class="kpi-label">${esc(k)}</div><div class="kpi-value wrap">${esc(val)}</div></div>`).join('');
}
function renderTabs() {
  document.getElementById('tabs').innerHTML = tabs.map(([id, label]) =>
    `<button class="${state.tab === id ? 'active' : ''}" onclick="state.tab='${id}'; selectTab()">${label}</button>`).join('');
}
function selectTab() {
  for (const [id] of tabs) document.getElementById(id).classList.toggle('active', state.tab === id);
  renderTabs();
}

function renderHarnessCard() {
  const ht = summary.harness_terminal || {};
  const b = summary.budget || {};
  const unfinished = !ht.run_end_seen;
  const stopBanner = unfinished && ht.stop_requested
    ? `<div class="alert warn">已请求停止，但 IPC 未出现 run_end —— <strong>请求停止 ≠ 已结束</strong>；运行可能仍在进行或进程中断。</div>` : '';
  const summaryBanner = unfinished && ht.run_summary_present
    ? `<div class="alert warn">存在已落盘的 run.json 摘要，但未观察到 run_end 事件：<strong>不据此判终局</strong>，状态按未终止处理（摘要见“原始文件”）。</div>` : '';
  const stopBanner2 = ht.state === 'stopped'
    ? `<div class="alert info">harness 因控制台停止而结束（takeover_reason=「用户从 Web 控制台停止」）。停止是 harness 事实，不代表 Case 通过。</div>` : '';
  return `<div class="card"><h2>① harness 终局（事实）</h2>${stopBanner}${summaryBanner}${stopBanner2}
    <table>
      ${row('状态', VERDICT_LABEL[summary.verdict] || summary.verdict)}
      ${row('run_end 事件', ht.run_end_seen)}
      ${row('run.json 摘要', ht.run_summary_present)}
      ${row('进程存活', ht.process_alive === null || ht.process_alive === undefined ? '未知' : ht.process_alive)}
      ${row('已声明完成', ht.finished)}
      ${row('完成摘要', ht.finish_summary)}
      ${row('接管原因', ht.takeover_reason)}
      ${row('终止原因', ht.reason)}
      ${row('步数 / token 预算', `${ht.steps ?? '—'} / ${b.token_budget ?? '—'}`)}
      ${row('预算耗尽 / 保险丝', `${b.exhausted} / ${b.loop_fuse_hit}`)}
      ${row('请求停止', ht.stop_requested)}
    </table>
    <div class="muted" style="margin-top:8px;font-size:12px">终局权威是 run_end 事件；run.json 只是已落盘摘要，二者的存在状态分开显示。</div>
  </div>`;
}
function renderCaseCard() {
  const ca = summary.case_acceptance || {};
  const cs = summary.case || null;
  const overall = ca.overall || 'unknown';
  if (!cs) return `<div class="card"><h2>② Case 验收（证据）</h2><div class="muted">未提供 Case；仅 harness 终局可得。</div></div>`;
  const cps = ca.checkpoints || [];
  return `<div class="card"><h2>② Case 验收（证据）</h2>
    <div class="cause-head">${badge('Case ' + cs.id)} ${badge(CASE_LABEL[overall] || overall, overall === 'pass' ? 'success' : (overall === 'fail' ? 'failed' : 'unknown'))}</div>
    <div class="muted" style="margin:4px 0 8px">${esc(ca.note || '')}</div>
    <table><tr><th>检查点</th><th>说明</th><th>判定</th><th>证据</th></tr>
      ${cps.length ? cps.map(cp => `<tr>
        <td class="mono">${esc(cp.id)}</td><td class="wrap">${esc(cp.description)}</td>
        <td>${badge(STATUS_LABEL[cp.status] || cp.status, cp.status)}</td>
        <td class="mono wrap">${esc((cp.evidence || []).join('；') || '—')}</td></tr>`).join('')
        : '<tr><td colspan="4" class="muted">无验收检查点。</td></tr>'}
    </table>
    <div class="muted" style="margin-top:8px;font-size:12px">harness finished ≠ Case 通过：无证据的检查点保持 unknown。</div>
  </div>`;
}
function renderTopThree() {
  const recs = (summary.recommendations || []).slice(0, 3);
  if (!recs.length) return `<div class="card"><h2>③ 诊断推断 · 优先项</h2><div class="muted">未发现需要修改的高优先级问题。</div></div>`;
  return `<div class="card"><h2>③ 诊断推断 · 优先项（非已证根因）</h2>${recs.map(r => `
    <div class="cause-card sev-${esc(r.priority)}">
      <div class="cause-head">${badge(r.id)} ${badge(r.priority)}</div>
      <div class="cause-what">${esc(r.title)}</div>
      <div class="cause-block">
        <span class="cause-label">怎么办</span><span class="wrap">${esc(r.recommendation)}</span>
        <span class="cause-label">验证</span><span class="wrap">${esc(r.verification)}</span>
      </div>
      ${(r.target_files || []).length ? `<div class="cause-src mono wrap">候选文件：${(r.target_files||[]).map(f => esc(f.path)).join(' · ')}</div>` : ''}
    </div>`).join('')}</div>`;
}
function renderConfigCard() {
  const cs = summary.case;
  if (!cs) return '';
  const preLabel = cs.preconditions_confirmed ? '前置条件（已人工确认）' : '前置条件（未经验证的假设）';
  return `<div class="card"><h2>Case 定义</h2>
    <div class="board-goal"><div><span class="cause-label">目标</span> <span class="base wrap">${esc(cs.goal)}</span></div></div>
    ${(cs.preconditions||[]).length ? `<h3>${esc(preLabel)}</h3><ul class="facts-list">${cs.preconditions.map(p=>`<li class="wrap">${esc(p)}</li>`).join('')}</ul>` : ''}
    ${(cs.safety_boundaries||[]).length ? `<h3>安全边界（作为任务约束传给 actor；运行期 safety 层不自动执行本条）</h3><ul class="facts-list">${cs.safety_boundaries.map(p=>`<li class="wrap">${esc(p)}</li>`).join('')}</ul>` : ''}
  </div>`;
}
function renderOverview() {
  const b = summary.budget || {};
  const issues = summary.data_issues || [];
  const alert = (b.token_budget && b.visible_used_tokens != null && b.visible_used_tokens >= b.token_budget)
    ? `<div class="alert warn">可见 token 用量已接近/超过预算（约 ${esc(b.visible_used_tokens)}/${esc(b.token_budget)}）。</div>` : '';
  const issueAlert = issues.length
    ? `<div class="alert warn">存在 ${esc(issues.length)} 条 JSONL 解析问题（坏行/坏类型），已跳过；详见“原始文件”。</div>` : '';
  document.getElementById('overview').innerHTML = `${alert}${issueAlert}
    <div class="alert info">三层独立裁定：① harness 终局是运行时事实；② Case 验收只看客观观测证据；③ 诊断是候选推断。单次工具错误不会把整场运行判为失败，harness finished 也不代表 Case 全部通过。</div>
    <div class="grid-2">${renderHarnessCard()}${renderCaseCard()}</div>
    <div style="margin-top:14px">${renderTopThree()}</div>
    <div style="margin-top:14px">${renderConfigCard()}</div>`;
}

// ---- Case tab: full acceptance + safety boundaries ------------------------
function renderCase() {
  const cs = summary.case || {};
  const ca = summary.case_acceptance || {};
  const preLabel = cs.preconditions_confirmed ? '前置条件（已人工确认）' : '前置条件（未经验证的假设）';
  document.getElementById('case').innerHTML = `
    <div class="card"><h2>Case：${esc(cs.title || '—')}</h2>
      <table>
        ${row('id', cs.id)}
        ${row('goal', cs.goal)}
        ${row('验收结论', CASE_LABEL[ca.overall] || ca.overall)}
        ${row('备注', cs.notes)}
      </table>
      <div class="grid-2" style="margin-top:12px">
        <div><h3>${esc(preLabel)}</h3>${(cs.preconditions||[]).length ? `<ul class="facts-list">${cs.preconditions.map(p=>`<li class="wrap">${esc(p)}</li>`).join('')}</ul>` : '<div class="muted">无</div>'}</div>
        <div><h3>安全边界（任务约束，不自动执行）</h3>${(cs.safety_boundaries||[]).length ? `<ul class="facts-list">${cs.safety_boundaries.map(p=>`<li class="wrap">${esc(p)}</li>`).join('')}</ul>` : '<div class="muted">无</div>'}</div>
      </div>
    </div>
    <div style="margin-top:14px">${renderCaseCard()}</div>`;
}

// ---- replay ----------------------------------------------------------------
function renderShot(image) {
  if (image && safeShot(image.path)) {
    const p = safeShot(image.path);
    const cap = image.screen_seq != null ? ('screen#' + image.screen_seq) : '截图';
    return `<div class="shot-col"><a href="${esc(p)}"><img class="shot" src="${esc(p)}" alt="${esc(cap)}" loading="lazy"></a><div class="shot-cap">${esc(cap)} · 点击看大图</div></div>`;
  }
  if (image && image.present) {
    const ref = image.reference ? `未验证参考图（ref=${esc(image.reference)}）` : '截图已回流但无本地引用';
    return `<div class="shot-col"><div class="shot-missing">${ref}</div></div>`;
  }
  return `<div class="shot-col"><div class="shot-missing">本步无截图</div></div>`;
}
function renderWindowsSummary(w) {
  if (!w || !w.windows || !w.windows.length) return '';
  const op = w.op_counts || {};
  const opBits = ['confirmed','likely','blocked','unknown','unspecified'].filter(k => (op[k] ?? 0) > 0).map(k => `${k} ${op[k]}`).join(' · ');
  const rows = w.windows.map(win => { const cover = win.covered_by ? ` covered_by=${win.covered_by}` : '';
    return `<tr><td class="mono">${esc(win.id || '?')}</td><td class="mono wrap">${esc(win.type || '—')}</td><td class="mono wrap">${esc(win.package || '—')}${esc(cover)}</td><td class="mono">${num(win.mark_count)}</td></tr>`; }).join('');
  return `<details><summary>窗口分组 (${esc(w.window_count)} 窗口${w.source ? ' · ' + esc(w.source) : ''})</summary>
    ${opBits ? `<div class="kv" style="margin-top:6px">op 分布：${esc(opBits)}</div>` : ''}
    <table style="margin-top:6px"><tr><th>窗口</th><th>类型</th><th>package</th><th>marks</th></tr>${rows}</table>
    ${(w.blocked_mark_ids || []).length ? `<div class="kv">blocked marks：<span class="mono">${esc((w.blocked_mark_ids||[]).join(', '))}</span></div>` : ''}</details>`;
}
function renderToolCall(tc) {
  const isErr = !!tc.error || ERROR_CLASSES.has(tc.class);
  const argStr = tc.args && Object.keys(tc.args || {}).length ? json(tc.args) : '{}';
  const intent = (tc.args && tc.args.intent) ? tc.args.intent : null;
  const note = (tc.args && tc.args.note) ? tc.args.note : null;
  return `<div class="toolcall ${isErr ? 'is-error' : ''}">
    <div class="toolcall-head">${badge(tc.tool || '?', isErr ? 'error' : '')} ${classBadge(tc.class)}
      ${tc.latency_ms != null ? badge(tc.latency_ms + 'ms') : ''}
      ${(tc.image && tc.image.screen_seq != null) ? badge('screen#' + tc.image.screen_seq, 'accent') : ''}
      ${(tc.image && tc.image.reference) ? badge('未验证 ref=' + tc.image.reference) : ''}
      ${(tc.windows && tc.windows.window_count != null) ? badge(tc.windows.window_count + ' 窗口', 'accent') : ''}</div>
    <div class="toolcall-body">
      ${intent ? `<div class="kv">intent：<span class="wrap">${esc(intent)}</span></div>` : ''}
      ${note ? `<div class="kv">note：<span class="wrap">${esc(note)}</span></div>` : ''}
      <div class="kv">参数</div><pre>${argStr}</pre>
      <div class="kv" style="margin-top:8px">回执</div>
      <pre class="result">${esc(tc.result_text || (tc.error ? ('⚠ ' + tc.error) : '（空返回）'))}</pre>
      ${renderWindowsSummary(tc.windows)}</div></div>`;
}
function renderStep(s) {
  const text = (s.model_text || '').trim();
  const calls = s.tool_calls || [];
  const shotImg = (calls.find(c => c.image && (c.image.path || c.image.present)) || {}).image || null;
  const ctx = s.context || {};
  const modelCalls = (s.model_tool_calls || []).map(mc => badge(mc.name || '?')).join(' ');
  const hitl = s.hitl || [];
  return `<div class="step"><div class="step-head">
      <span class="step-no">STEP ${esc(s.step ?? '?')}</span>
      ${modelCalls || '<span class="muted">（无工具调用）</span>'}
      ${ctx.image_message_count != null ? badge('图消息 ' + ctx.image_message_count) : ''}
      ${ctx.pruned_screen_count ? badge('剪除 ' + ctx.pruned_screen_count) : ''}
      ${ctx.context_chars != null ? badge(ctx.context_chars + ' 字') : ''}
      ${ctx.taskdoc_present ? badge('TaskDoc✓', 'success') : ''}</div>
    <div class="step-body">${renderShot(shotImg)}
      <div class="detail-col">
        <div class="kv">模型可见文本（实际返回内容，非隐藏思考）</div>
        <div class="think ${text ? '' : 'empty'}">${text ? esc(text) : '（本步模型未返回文本）'}</div>
        ${calls.length ? calls.map(renderToolCall).join('') : '<div class="muted">本步没有工具调用（可能是终局消息）。</div>'}
        ${hitl.length ? `<div class="alert info">HITL：${hitl.map(h => esc((h.decision || '') + ' — ' + (h.requested_action || ''))).join('；')}</div>` : ''}
      </div></div></div>`;
}
function renderReplay() {
  const replay = summary.replay || [];
  const host = document.getElementById('replay');
  if (!replay.length) { host.innerHTML = `<div class="card"><h2>逐步回放</h2><div class="muted">无回放数据（evidence 未记录 model_response/tool 事件）。</div></div>`; return; }
  host.innerHTML = `<div class="alert info">每步 = 真实截图（点击看大图）+ 模型可见文本 + 工具调用/参数（含 intent/note）+ 回执全文 + 延迟。截图落盘于 <span class="mono">screenshots/</span>。</div>${replay.map(renderStep).join('')}`;
}

// ---- problems + safety + finish verifier + hitl ---------------------------
function renderProblems() {
  const th = summary.tool_health || {};
  const byTool = th.by_tool || {};
  const fg = summary.finish_gate || {};
  const fv = summary.finish_verifier || {};
  const h = summary.hitl || {};
  const safety = summary.safety || {};
  const ce = summary.context_errors || {};
  const errAgg = {};
  for (const st of Object.values(byTool)) for (const [cls, n] of Object.entries(st.error_classes || {})) errAgg[cls] = (errAgg[cls] || 0) + n;
  const errRows = Object.entries(errAgg).sort((a,b) => b[1]-a[1]);
  const unresolved = h.unresolved_prompts || [];
  document.getElementById('problems').innerHTML = `
    <div class="card"><h2>工具错误分类（单项错误 ≠ 整场失败）</h2>
      ${errRows.length ? `<table><tr><th>class</th><th>次数</th></tr>${errRows.map(([cls, n]) => `<tr><td>${classBadge(cls)}</td><td class="mono">${n}</td></tr>`).join('')}</table>`
        : '<div class="muted">全程无被分类为错误的工具返回。</div>'}</div>
    <div class="card" style="margin-top:14px"><h2>安全预警（wary）</h2>
      <table>${row('预警次数', safety.count)}${row('模式', safety.mode || '—')}</table>
      ${safety.note ? `<div class="muted" style="margin-top:6px">${esc(safety.note)}</div>` : ''}
      ${(safety.warnings||[]).length ? `<table style="margin-top:8px"><tr><th>step</th><th>tool</th><th>预警</th></tr>${safety.warnings.map(w=>`<tr><td class="mono">${num(w.step)}</td><td class="mono">${esc(w.tool)}</td><td class="wrap">${esc(w.text)}</td></tr>`).join('')}</table>` : '<div class="muted" style="margin-top:8px">无安全预警。</div>'}</div>
    <div class="card" style="margin-top:14px"><h2>完成门与独立验收器</h2>
      <table>${row('finish 尝试 / 被接受', `${fg.attempted} / ${fg.accepted}`)}
        ${row('被开放项拦截', fg.blocked_by_open_items)}
        ${row('复核包 / 确认', `${fv.review_packets} / ${fv.confirmed}`)}
        ${row('验收驳回次数', fv.rejection_count)}
        ${row('反复驳回转接管', fv.dispute_takeover)}
        ${row('验收器状态', fv.verifier_status)}
        ${row('验收器状态来源', fv.verifier_status_source)}</table>
      <div class="muted" style="margin-top:6px;font-size:12px">${esc(fv.note || '')}</div>
      ${(fv.rejections||[]).length ? `<table style="margin-top:8px"><tr><th>step</th><th>驳回</th></tr>${fv.rejections.map(r=>`<tr><td class="mono">${num(r.step)}</td><td class="wrap">${esc(r.message)}</td></tr>`).join('')}</table>` : ''}</div>
    <div class="card" style="margin-top:14px"><h2>HITL 事件（未决不自动批准）</h2>
      <table>${row('中断次数', h.interrupts)}
        ${row('批准 / 拒绝 / 应答', `${h.approvals ?? 0} / ${h.rejections ?? 0} / ${h.responds ?? 0}`)}
        ${row('ask_user / take_over', `${h.ask_user_count ?? 0} / ${h.take_over_count ?? 0}`)}
        ${row('已提交 / 已消费 / 未消费', `${h.submitted_count ?? 0} / ${h.consumed_count ?? 0} / ${h.unconsumed_count ?? 0}`)}
        ${row('未决提示', unresolved.length)}</table>
      ${unresolved.length ? `<div class="alert warn">存在未决 HITL 提示（未自动批准）：${unresolved.map(esc).join('；')}</div>` : ''}
      ${(h.decisions||[]).length ? `<table style="margin-top:8px"><tr><th>step</th><th>tool</th><th>决定</th></tr>${(h.decisions||[]).map(d=>`<tr><td class="mono">${num(d.step)}</td><td class="mono">${esc(d.tool)}</td><td>${badge(d.decision)}</td></tr>`).join('')}</table>` : ''}</div>
    <div class="card" style="margin-top:14px"><h2>上下文错误</h2>
      ${(ce.errors||[]).length ? `<table><tr><th>step</th><th>kind</th><th>信息</th></tr>${(ce.errors||[]).map(e=>`<tr><td class="mono">${num(e.step)}</td><td class="mono">${esc(e.kind)}</td><td class="wrap">${esc(e.message)}</td></tr>`).join('')}</table>` : '<div class="muted">无上下文错误事件。</div>'}</div>`;
}

// ---- dimensions -----------------------------------------------------------
function renderWindowingCard(w) {
  if (!w || !w.present) return `<div class="card"><h2>窗口结构</h2><div class="muted">本 run 的 marks 为平铺格式（未启用窗口分组，或旧 run）。</div></div>`;
  const op = w.op_counts || {}; const types = w.window_types || {}; const blockedTaps = w.blocked_taps || [];
  const typeChips = Object.entries(types).map(([t, n]) => badge(`${t}×${n}`)).join(' ') || '<span class="muted">—</span>';
  const opChips = ['confirmed','likely','blocked','unknown','unspecified'].map(k => badge(`${k} ${op[k] ?? 0}`, k === 'blocked' ? 'blocked' : (k === 'confirmed' ? 'success' : ''))).join(' ');
  return `<div class="card"><h2>窗口结构</h2><table>${row('窗口化观测数', w.windowed_observations)}${row('峰值窗口数', w.peak_window_count)}</table>
    <div class="cause-head" style="margin-top:8px">${opChips}</div><div class="kv" style="margin-top:6px">窗口类型分布</div><div class="chip-cloud">${typeChips}</div>
    ${blockedTaps.length ? `<div class="alert warn" style="margin-top:10px">点击了 op=blocked 的 mark ${blockedTaps.length} 次：${blockedTaps.map(t => esc(`step ${t.step} ${t.tool}→${t.target_mark_id}`)).join('；')}</div>` : ''}</div>`;
}
function renderDimensions() {
  const c = summary.context || {}; const th = summary.tool_health || {}; const g = summary.grounding || {};
  const v = summary.visual || {}; const w = summary.windowing || {}; const m = summary.model || {}; const usage = m.token_usage || {};
  const b = summary.budget || {}; const fb = summary.fallback || [];
  const byTool = th.by_tool || {}; const maxLat = Math.max(1, ...Object.values(byTool).map(st => st.p95_latency_ms || 0));
  const identity = m.identity || [];
  document.getElementById('dimensions').innerHTML = `<div class="grid-2">
    <div class="card"><h2>模型身份与用量</h2><table>
      ${row('调用次数', m.calls)}
      ${row('requested', (m.requested_models||[]).join(', ') || '未上报')}
      ${row('actual', (m.actual_models||[]).join(', ') || '未上报')}
      ${row('输入 / 输出 token', `${usage.reported === false ? '未上报' : (usage.input_tokens ?? '未上报')} / ${usage.reported === false ? '未上报' : (usage.output_tokens ?? '未上报')}`)}
      ${row('cache read / write', `${usage.cache_read_tokens ?? 'null'} / ${usage.cache_write_tokens ?? 'null'}`)}
      ${row('总 token', usage.reported === false ? '未上报' : (usage.partial ? 'partial（缺输入/输出，保留 null）' : (usage.total_tokens ?? '未上报')))}
      ${(usage.coverage) ? row('字段覆盖（调用数）', `in ${usage.coverage.input_tokens_calls ?? 0}/${usage.coverage.calls ?? 0} · out ${usage.coverage.output_tokens_calls ?? 0}/${usage.coverage.calls ?? 0} · cacheR ${usage.coverage.cache_read_tokens_calls ?? 0} · cacheW ${usage.coverage.cache_write_tokens_calls ?? 0}`) : ''}</table>
      <div class="muted" style="font-size:12px">缺失即 null/未上报，不补零。requested 来自请求绑定，actual 仅取 provider 上报。</div>
      <h3>预算</h3><table>
        ${row('token 预算', b.token_budget)}
        ${row('visible_used（actor 上报）', b.visible_usage_reported ? (b.visible_used_tokens ?? 'partial') : '未上报')}
        ${row('ledger（含 aux/估算）', b.ledger_available ? (b.ledger_used_tokens ?? '—') : 'unknown（未导出）')}
        ${row('预算耗尽 / 保险丝', `${b.exhausted} / ${b.loop_fuse_hit}`)}</table>
      ${fb.length ? `<h3>模型降级（真实 model_fallback 事件）</h3><table><tr><th>stage</th><th>role</th><th>requested→actual</th><th>reason</th><th>outcome</th></tr>${fb.map(f=>`<tr><td class="mono">${esc(f.stage)}</td><td class="mono">${esc(f.role)}</td><td class="mono wrap">${esc(f.requested)}→${esc(f.actual)}</td><td class="mono wrap">${esc(f.reason)}</td><td>${esc(f.outcome)}</td></tr>`).join('')}</table>` : '<div class="muted" style="margin-top:6px">无 model_fallback 事件。</div>'}</div>
    <div class="card"><h2>上下文卫生</h2><table>
      ${row('峰值消息数', c.peak_message_count)}${row('峰值图像消息', c.peak_image_messages)}
      ${row('累计剪除截图', c.pruned_screen_total)}${row('TaskDoc 每步钉入', c.taskdoc_pinned_every_step)}
      ${row('平均上下文字符', c.avg_context_chars)}</table>
      ${(c.peak_image_messages || 0) > 2 ? '<div class="alert warn">峰值图像消息 &gt; 2：图像剪裁可能失效（默认 image_keep=2）。</div>' : ''}</div>
    <div class="card"><h2>视觉回流</h2><table>${row('带截图的工具返回', v.tool_results_with_image)}${row('累计截图字节', v.total_image_bytes)}${row('首 / 末截图步', `${v.first_image_step ?? '—'} / ${v.last_image_step ?? '—'}`)}</table></div>
    ${renderWindowingCard(w)}
    <div class="card"><h2>Grounding</h2><table>
      ${row('by_mark_id / by_description', `${(g.mark_addressing||{}).by_mark_id ?? 0} / ${(g.mark_addressing||{}).by_description ?? 0}`)}
      ${row('解析失败 ambiguous/stale/no_match', `${(g.resolve_failures||{}).ambiguous ?? 0} / ${(g.resolve_failures||{}).stale ?? 0} / ${(g.resolve_failures||{}).no_match ?? 0}`)}
      ${row('locate calls/ok/no_match/err', `${(g.locate||{}).calls ?? 0} / ${(g.locate||{}).success ?? 0} / ${(g.locate||{}).no_match ?? 0} / ${(g.locate||{}).provider_error ?? 0}`)}</table></div>
    <div class="card" style="grid-column:1 / -1"><h2>工具健康与延迟</h2>
      <div class="cause-head">${badge('调用 ' + (th.total_calls ?? 0))} ${badge('错误 ' + (th.total_errors ?? 0), (th.total_errors||0) ? 'failed' : '')} ${badge('错误率 ' + Math.round((th.error_rate||0)*100) + '%')}</div>
      <table><tr><th>tool</th><th>calls</th><th>ok</th><th>error</th><th>error classes</th><th>avg ms</th><th>p95 ms</th><th>p95</th></tr>
        ${Object.keys(byTool).length ? Object.entries(byTool).map(([tool, st]) => `<tr><td class="mono">${esc(tool)}</td><td class="mono">${num(st.calls)}</td><td class="mono">${num(st.ok)}</td><td class="mono ${st.error ? 'severity-P0' : ''}">${num(st.error)}</td><td class="mono wrap">${esc(Object.entries(st.error_classes||{}).map(([k,cc]) => `${k}×${cc}`).join(', ') || '—')}</td><td class="mono">${num(st.avg_latency_ms)}</td><td class="mono">${num(st.p95_latency_ms)}</td><td style="min-width:120px"><div class="bar"><span style="width:${Math.round(100*(st.p95_latency_ms||0)/maxLat)}%"></span></div></td></tr>`).join('') : '<tr><td colspan="8" class="muted">无工具调用</td></tr>'}</table></div>
  </div>`;
}

// ---- source attribution (inference) ---------------------------------------
function renderFiles(files) {
  return (files || []).map(file => {
    const anchors = (file.anchors || []).slice(0, 3).map(a => `${a.symbol}:${a.line}`).join(', ');
    const missing = file.exists === false ? ' <span class="severity-P0">(缺失)</span>' : '';
    return `<div class="mono wrap">${esc(file.path)}${missing}${anchors ? '<br><span class="muted">' + esc(anchors) + '</span>' : ''}</div>`;
  }).join('');
}
function renderSource() {
  const rows = summary.findings || [];
  const caveat = (summary.diagnosis || {}).caveat || '';
  document.getElementById('source').innerHTML = `<div class="alert warn">${esc(caveat)}</div>
    <div class="card"><h2>源码归因（候选）</h2><table>
      <tr><th>优先级</th><th>层级</th><th>现象</th><th>次数</th><th>候选文件 / 符号</th><th>建议 / 验证</th></tr>
      ${rows.length ? rows.map(f => `<tr>
        <td class="severity-${esc(f.severity)}">${esc(f.severity)}</td><td class="mono">${esc(f.layer)}</td>
        <td class="wrap">${esc(f.title)}<br><span class="muted mono">${esc((f.examples||[]).slice(0,1).join(''))}</span></td>
        <td class="mono">${num(f.count)}</td><td>${renderFiles(f.files || [])}</td>
        <td class="wrap">${esc(f.suggestion)}<br><span class="muted">${esc(f.verify)}</span></td></tr>`).join('') : '<tr><td colspan="6" class="muted">未发现被归因的错误类别。</td></tr>'}</table></div>
    <div class="card" style="margin-top:14px"><h2>全部建议（候选）</h2><table>
      <tr><th>ID</th><th>优先级</th><th>建议</th><th>候选文件</th><th>验证</th></tr>
      ${(summary.recommendations||[]).length ? (summary.recommendations||[]).map(r => `<tr><td class="mono">${esc(r.id)}</td><td class="severity-${esc(r.priority)}">${esc(r.priority)}</td><td class="wrap"><strong>${esc(r.title)}</strong><br>${esc(r.recommendation)}</td><td>${renderFiles(r.target_files || [])}</td><td class="wrap">${esc(r.verification)}</td></tr>`).join('') : '<tr><td colspan="5" class="muted">无建议。</td></tr>'}</table></div>`;
}

// ---- raw -------------------------------------------------------------------
function renderRaw() {
  const a = summary.artifacts || {};
  const links = [
    ['summary.json', a.summary || 'summary.json'],
    ['events.jsonl', 'events.jsonl'],
    ['evidence.jsonl', a.evidence || summary.evidence_stream || 'evidence.jsonl'],
    ['run.json', 'run.json'],
    ['control.jsonl', 'control.jsonl'],
    ['spec.json', 'spec.json'],
    ['traces/', summary.trace || 'traces'],
  ];
  const notes = summary.notes || [];
  const issues = summary.data_issues || [];
  document.getElementById('raw').innerHTML = `
    ${notes.length ? `<div class="alert info">${notes.map(esc).join('<br>')}</div>` : ''}
    ${issues.length ? `<div class="alert warn">JSONL 解析问题（${esc(issues.length)}）：${issues.map(i => esc(`${i.source}#${i.line ?? '?'}:${i.reason}`)).join('；')} —— 坏行已跳过，分析基于其余可见证据。</div>` : ''}
    <div class="card"><h2>原始文件（本地相对路径）</h2><table><tr><th>文件</th><th>路径</th></tr>
      ${links.map(([k, p]) => `<tr><td class="mono">${esc(k)}</td><td class="wrap">${safeLink(p, p)}</td></tr>`).join('')}</table>
      <div class="muted" style="margin-top:8px;font-size:12px">只渲染本地相对路径；远程 URL 与绝对路径被丢弃。</div></div>
    <div class="grid-2" style="margin-top:14px">
      <div class="card"><h2>Summary JSON</h2><pre>${json(summary)}</pre></div>
      <div class="card"><h2>Evidence 事件（前 200）</h2><pre>${json(evidence.slice(0, 200))}</pre></div></div>`;
}
render();
</script>
</body>
</html>
"""


__all__ = ["render_html", "sanitize_summary_paths", "HTML_TEMPLATE"]
