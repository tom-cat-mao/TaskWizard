"""WP-DOC run-bound HTML tools, C2 mount, episode, and trace contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    assemble_capabilities,
    build_capability_registry,
)
from phone_agent.v2.experience import ExperienceWriter
from phone_agent.v2.middleware.trace import TraceMiddleware
from phone_agent.v2.prompts import get_deliverable_prompt
from phone_agent.v2.tools.deliverable import (
    MAX_DOCUMENT_BYTES,
    make_deliverable_tools,
)


def _tools(run_id: str, root: Path, on_success=None):
    return {
        item.name: item
        for item in make_deliverable_tools(run_id, root, on_success=on_success)
    }


def test_write_and_update_derive_only_run_path_and_report_utf8_bytes(tmp_path):
    seen = []
    tools = _tools("run-42", tmp_path / "out", seen.append)
    assert set(tools["write_document"].args) == {
        "title",
        "html",
        "intent",
        "note",
    }
    assert set(tools["update_document"].args) == {"html", "intent", "note"}

    first = "<!doctype html><title>攻略</title>"
    receipt = tools["write_document"].invoke(
        {"title": "攻略", "html": first, "intent": "交付攻略"}
    )
    target = tmp_path / "out/run-42.html"
    assert target.read_text(encoding="utf-8") == first
    assert str(target) in receipt
    assert f"{len(first.encode('utf-8'))} bytes" in receipt

    second = "<!doctype html><title>更新</title>"
    receipt = tools["update_document"].invoke(
        {"html": second, "intent": "修订攻略", "note": "补充价格"}
    )
    assert target.read_text(encoding="utf-8") == second
    assert "已更新文档" in receipt
    assert seen == [str(target), str(target)]


def test_create_existing_and_update_missing_fail_closed(tmp_path):
    tools = _tools("run-a", tmp_path)
    target = tmp_path / "run-a.html"
    target.write_text("original", encoding="utf-8")

    receipt = tools["write_document"].invoke(
        {"title": "x", "html": "replacement", "intent": "write"}
    )
    assert receipt.startswith("error:")
    assert "update_document" in receipt
    assert target.read_text(encoding="utf-8") == "original"

    missing = _tools("run-missing", tmp_path)
    receipt = missing["update_document"].invoke(
        {"html": "replacement", "intent": "update"}
    )
    assert receipt.startswith("error:")
    assert "write_document first" in receipt
    assert not (tmp_path / "run-missing.html").exists()


def test_size_limit_and_invalid_run_id_cannot_write_or_escape(tmp_path):
    root = tmp_path / "out"
    exact = "x" * MAX_DOCUMENT_BYTES
    exact_receipt = _tools("run-exact", root)["write_document"].invoke(
        {"title": "exact", "html": exact, "intent": "write"}
    )
    assert exact_receipt.startswith("OK.")
    assert (root / "run-exact.html").stat().st_size == MAX_DOCUMENT_BYTES

    tools = _tools("run-big", root)
    too_large = "文" * (MAX_DOCUMENT_BYTES // 3 + 1)
    receipt = tools["write_document"].invoke(
        {"title": "big", "html": too_large, "intent": "write"}
    )
    assert receipt.startswith("error:")
    assert "exceeds 262144 byte limit" in receipt
    assert not (root / "run-big.html").exists()

    escape = _tools("../escape", root)
    receipt = escape["write_document"].invoke(
        {"title": "x", "html": "safe", "intent": "write"}
    )
    assert receipt.startswith("error:")
    assert not (tmp_path / "escape.html").exists()


def test_update_rejects_symlink_without_touching_external_file(tmp_path):
    external = tmp_path / "external.html"
    external.write_text("outside", encoding="utf-8")
    root = tmp_path / "out"
    root.mkdir()
    (root / "run-link.html").symlink_to(external)

    receipt = _tools("run-link", root)["update_document"].invoke(
        {"html": "replacement", "intent": "update"}
    )
    assert receipt.startswith("error:")
    assert "symlink" in receipt
    assert external.read_text(encoding="utf-8") == "outside"


def _config(enabled: bool):
    return SimpleNamespace(
        taskdoc_enabled=False,
        safety_mode="off",
        compact_enabled=False,
        finish_verify="off",
        deliverable_enabled=enabled,
        app_kb_enabled=False,
        dream_mode="off",
        experience_enabled=False,
        memory_rag="off",
    )


def _c2_context(tmp_path):
    tools = make_deliverable_tools("run-c2", tmp_path)
    return CapabilityAssemblyContext(
        {
            "deliverable_tools_factory": lambda: tools,
            "deliverable_prompt_provider": lambda: get_deliverable_prompt("cn"),
        }
    )


def test_c2_on_off_and_release_leave_zero_residue(tmp_path):
    ctx = _c2_context(tmp_path)
    assemble_capabilities(build_capability_registry(_config(True)), ctx)
    assert {tool.name for tool in ctx.tools} == {
        "write_document",
        "update_document",
    }
    assert len(ctx.prompt_providers) == 1
    assert "单页 HTML" in str(ctx.prompt_providers[0]())

    assemble_capabilities(build_capability_registry(_config(False)), ctx)
    assert ctx.tools == []
    assert ctx.prompt_providers == []

    never_mounted = _c2_context(tmp_path)
    assemble_capabilities(build_capability_registry(_config(False)), never_mounted)
    assert ctx.tools == never_mounted.tools
    assert ctx.prompt_providers == never_mounted.prompt_providers
    assert "write_document" in get_deliverable_prompt("cn")
    assert "write_document" in get_deliverable_prompt("en")


def test_episode_optional_path_and_recall_compatible_shape(tmp_path):
    writer = ExperienceWriter(tmp_path / "experience")
    common = dict(
        schema_v=1,
        ts_start=1.0,
        ts_end=2.0,
        time_of_day="morning",
        day_of_week=1,
        device_scope="device:test",
        goal_text="写攻略",
        apps=[],
        success=True,
        reason="finished",
        steps=2,
        tokens_total=10,
        tokens_by_role={"actor": 10},
        warnings=0,
        takeover=None,
        verifier="skipped",
        capabilities={"deliverable": "active"},
        injected_lessons=[],
    )
    legacy = writer.append_outcome(run_id="legacy", **common)
    current = writer.append_outcome(
        run_id="with-doc",
        deliverable_path="outputs/deliverables/with-doc.html",
        **common,
    )
    assert legacy["deliverable_path"] is None
    assert current["deliverable_path"] == "outputs/deliverables/with-doc.html"

    from phone_agent.v2.recall import read_episode_events

    recalled = read_episode_events(writer.events_path)
    assert [item["run_id"] for item in recalled] == ["legacy", "with-doc"]


def test_trace_records_document_call_without_any_html_body(tmp_path):
    body = "<!doctype html><title>TOP_SECRET_BODY_9274</title>" + "x" * 100
    writer = ExperienceWriter(tmp_path / "experience")
    middleware = TraceMiddleware(
        "run-doc", trace_dir=str(tmp_path), experience_writer=writer
    )
    request = SimpleNamespace(
        tool_call={
            "name": "write_document",
            "args": {"title": "report", "html": body, "intent": "deliver"},
        }
    )
    middleware.on_tool_execute(
        request,
        lambda _request: SimpleNamespace(
            content="OK. created outputs/deliverables/run-doc.html (148 bytes)"
        ),
    )
    raw = (tmp_path / "run-doc.jsonl").read_text(encoding="utf-8")
    assert "TOP_SECRET_BODY_9274" not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    call = next(item for item in events if item["event"] == "tool_call")
    assert call["args_redacted"]["html"] == {
        "type": "text",
        "omitted": True,
        "bytes": len(body.encode("utf-8")),
    }
    event = json.loads(writer.events_path.read_text(encoding="utf-8"))
    assert event["tool"] == "write_document"
    assert event["result_class"] == "ok"
    assert "TOP_SECRET_BODY_9274" not in writer.events_path.read_text(
        encoding="utf-8"
    )


def test_agent_outcome_links_only_successfully_recorded_deliverable(tmp_path):
    from phone_agent.v2.agent import RunResult, ThinPhoneAgent

    agent = ThinPhoneAgent.__new__(ThinPhoneAgent)
    agent.run_id = "run-linked"
    agent._experience_writer = ExperienceWriter(tmp_path / "experience")
    agent.usage_ledger = None
    agent.session = SimpleNamespace(
        launched_apps=[], takeover_reason=None, finish_verifier="skipped"
    )
    agent._safety_warning = None
    agent._run_capabilities = {"deliverable": "active"}
    agent._actually_injected_lesson_ids = []
    agent._deliverable_path = None

    target = tmp_path / "out/run-linked.html"
    tools = _tools("run-linked", target.parent, agent._record_deliverable_path)
    receipt = tools["write_document"].invoke(
        {"title": "report", "html": "<html></html>", "intent": "write"}
    )
    assert receipt.startswith("OK.")

    agent._append_experience_outcome(
        "写报告",
        RunResult(True, "finished", 1),
        ts_start=1.0,
        ts_end=2.0,
        device_scope="device:test",
    )
    outcome = json.loads(agent._experience_writer.events_path.read_text(encoding="utf-8"))
    assert outcome["deliverable_path"] == str(target)
