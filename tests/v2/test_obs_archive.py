"""WP-A observation archive + recall: text-only archive, FTS search, paging.

Covered against the real :class:`PhoneSession` driven by a fake DeviceFactory
(no device / MLX / network):

    - every committed successful observation appends exactly one archive record
      whose ``text`` is byte-identical to the model-facing ``[OBS]`` block, and
      no screenshot payload/base64 ever reaches the archive file;
    - failed observations (no commit) are never archived;
    - the derived FTS5 index answers ``search_screens`` and rebuilds itself from
      the JSONL when deleted (derived state);
    - ``recall_screen`` pages by line and every historical mark id is rendered
      non-addressable (``历史:ax_1@e1（已失效）``);
    - the capability off path mounts nothing and the fold placeholder stays
      byte-identical to the historic text;
    - archive/index/recall failures are fail-open (observe still commits, tools
      return honest error text);
    - retention keeps the newest N runs and deletes the rest with sidecars.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from phone_agent.v2.capabilities import (
    CapabilityAssemblyContext,
    CapabilityRegistry,
    assemble_capabilities,
    build_capability_registry,
)
from phone_agent.v2.middleware.images import ContextPrunerService
from phone_agent.v2.obs_archive import (
    RECALL_HINT,
    ObsArchive,
    ObsArchiveError,
    build_obs_archive,
    invalidate_marks,
    prune_obs_archive_runs,
    read_archive_records,
)
from phone_agent.v2.session import PhoneSession, ScreenshotError
from phone_agent.v2.tools._obs import format_observation_text
from phone_agent.v2.tools.obs_archive import make_obs_archive_tools


# --------------------------------------------------------------------------
# Fakes: a screenshot, a foreground observation, and a scriptable device.
# --------------------------------------------------------------------------
class FakeShot:
    def __init__(self, payload: str = "SHOT-PAYLOAD", *, valid: bool = True) -> None:
        self.base64_data = payload
        self.width = 1080
        self.height = 2400
        self.mime_type = "image/png"
        self.is_valid = valid
        self.failure_code = None if valid else "screenshot_unavailable"


class FakeForeground:
    def __init__(self, component: str = "com.example.app/.Main") -> None:
        self.component_name = component
        self.package_name = component.split("/", 1)[0]
        self.display_name = self.package_name


_SETTINGS_XML = (
    "<hierarchy>"
    '<node text="WLAN" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,100][1080,300]" />'
    '<node text="蓝牙" class="android.widget.TextView" clickable="true" '
    'enabled="true" bounds="[0,300][1080,500]" />'
    "</hierarchy>"
)


class FakeConfig:
    device_id = None
    observe_settle_ms = 0
    accessibility_max_marks = 80
    accessibility_timeout = 3.0
    grounding_provider = "accessibility"
    locateanything_max_size = 960
    locateanything_context_max_chars = 200
    locate_max_size = 0
    scope_padding_ratio = 0.05
    locateanything_model = None
    marks_windowed = "off"


class FakeDeviceFactory:
    """Scriptable device: screenshots, foreground, and a UiAutomator dump."""

    def __init__(self, *, xml: str = _SETTINGS_XML, screenshot_valid: bool = True):
        self._xml = xml
        self._screenshot_valid = screenshot_valid
        self.screenshot_calls = 0

    def get_screenshot(self, device_id=None, timeout=10):
        self.screenshot_calls += 1
        return FakeShot(f"SHOT-{self.screenshot_calls}", valid=self._screenshot_valid)

    def dump_uiautomator_xml(self, device_id=None, timeout=None):
        return self._xml

    def get_foreground_app(self, device_id=None):
        return FakeForeground()


class RecordingSession(PhoneSession):
    """Real session plus the trace-recorder slot the archive diagnoses into."""

    def __init__(self, config, device_factory):
        super().__init__(config, device_factory=device_factory)
        self.trace_events: list[tuple[str, dict]] = []

        def record(event: str, **payload) -> None:
            self.trace_events.append((event, payload))

        self.resolution_trace_recorder = record


def _session(**kwargs) -> RecordingSession:
    return RecordingSession(FakeConfig(), FakeDeviceFactory(**kwargs))


def _attach(session: RecordingSession, root: Path, run_id: str = "run1") -> ObsArchive:
    archive = ObsArchive(root, run_id, keep_runs=20)
    session.obs_archive_sink = archive
    return archive


def _archive_records(archive: ObsArchive) -> list[dict]:
    return read_archive_records(archive.jsonl_path)


# Mark ids are invalid iff every occurrence is wrapped exactly once.
_MARK_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:#\d+)?@e\d+")


def _assert_all_marks_invalidated(text: str) -> None:
    matches = list(_MARK_ID_RE.finditer(text))
    assert matches, "expected at least one mark id in the recalled content"
    for match in matches:
        assert text[max(0, match.start() - 3) : match.start()] == "历史:"
        assert text[match.end() : match.end() + 5] == "（已失效）"


# --------------------------------------------------------------------------
# archive-on-observe (P0 #15 single producer; P0 #6 text only)
# --------------------------------------------------------------------------
def test_committed_observation_archives_exact_obs_text_without_pixels(tmp_path):
    session = _session()
    archive = _attach(session, tmp_path / "obs")

    observation = session.observe()

    records = _archive_records(archive)
    assert len(records) == 1
    record = records[0]
    expected_text = format_observation_text(session, observation)
    assert record["text"] == expected_text
    assert record["schema_v"] == 1
    assert record["run_id"] == "run1"
    assert record["screen_seq"] == observation.screen_seq == 1
    assert record["epoch"] == observation.epoch == 1
    assert record["app"] == observation.current_app
    assert record["package"] == "com.example.app"
    assert isinstance(record["ts"], float) and record["ts"] > 0
    assert record["text"].startswith("[OBS] app=")
    assert "marks (" in record["text"]

    raw = archive.jsonl_path.read_text(encoding="utf-8")
    assert "SHOT-1" not in raw
    assert "base64" not in raw


def test_archive_keeps_the_window_annotated_digest_shape(tmp_path):
    """The archived text is the shared renderer output, digest annotations included."""

    session = _session()
    archive = _attach(session, tmp_path / "obs")
    observation = session.observe()

    record = _archive_records(archive)[0]
    assert record["text"] == format_observation_text(session, observation)
    assert "WLAN" in record["text"] and "蓝牙" in record["text"]
    # Window grouping headers and actionability annotations are part of the
    # model-facing digest and therefore part of the archive.
    assert "W1" in record["text"] and "W2" in record["text"]
    assert "op=likely" in record["text"]


def test_failed_observation_is_never_archived(tmp_path):
    session = _session(screenshot_valid=False)
    archive = _attach(session, tmp_path / "obs")

    with pytest.raises(ScreenshotError):
        session.observe()

    assert not archive.jsonl_path.exists()
    assert session.marks == {}


def test_capability_off_builds_no_archive_and_writes_nothing(tmp_path):
    config = SimpleNamespace(obs_archive="off", obs_archive_dir=str(tmp_path / "obs"))
    assert build_obs_archive(config, "run1") is None

    session = _session()
    session.observe()
    assert not (tmp_path / "obs").exists()
    assert not session.trace_events


def test_build_obs_archive_from_config_is_lazy_and_uses_configured_dir(tmp_path):
    config = SimpleNamespace(
        obs_archive="on",
        obs_archive_dir=str(tmp_path / "obs"),
        obs_archive_keep_runs=3,
    )

    archive = build_obs_archive(config, "run1")

    assert isinstance(archive, ObsArchive)
    assert archive.root_dir == tmp_path / "obs"
    assert archive.keep_runs == 3
    # Lazy: assembling the capability itself performs no filesystem writes.
    assert not archive.root_dir.exists()


# --------------------------------------------------------------------------
# FTS search + derived-index rebuild
# --------------------------------------------------------------------------
def test_search_finds_matching_frames_and_rebuilds_index(tmp_path):
    session = _session(
        xml=(
            "<hierarchy>"
            '<node text="WLAN" class="android.widget.TextView" clickable="true" '
            'enabled="true" bounds="[0,100][1080,300]" />'
            "</hierarchy>"
        )
    )
    archive = _attach(session, tmp_path / "obs")
    session.observe()
    session.device_factory._xml = _SETTINGS_XML.replace(
        'text="WLAN"', 'text="蓝牙设置"'
    )
    session.observe()

    hits = archive.search("蓝牙设置")
    assert [hit.screen_seq for hit in hits] == [2]
    assert hits[0].app == "com.example.app"
    assert "蓝牙设置" in hits[0].snippet

    # Derived state: delete the db and the next query rebuilds from the JSONL.
    archive.close()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{archive.db_path}{suffix}").unlink(missing_ok=True)
    rebuilt = archive.search("WLAN")
    assert [hit.screen_seq for hit in rebuilt] == [1]


def test_search_empty_query_and_empty_archive_return_nothing(tmp_path):
    archive = ObsArchive(tmp_path / "obs", "run1")
    assert archive.search("") == []
    assert archive.search("任意词") == []


# --------------------------------------------------------------------------
# recall_screen paging + mark invalidation (P0 #2)
# --------------------------------------------------------------------------
def test_recall_screen_pages_by_line_and_invalidates_marks(tmp_path):
    session = _session()
    archive = _attach(session, tmp_path / "obs")
    session.observe()

    full = archive.page(1, offset=0, limit=80)
    lines = full.text.splitlines()
    # [OBS] header + windowed digest lines (window headers + one mark each).
    assert full.total_lines == len(lines) == 6
    assert full.next_offset is None

    pages = [
        archive.page(1, offset=offset, limit=2) for offset in range(0, len(lines), 2)
    ]
    assert [len(page.text.splitlines()) for page in pages] == [2, 2, 2]
    assert [page.next_offset for page in pages] == [2, 4, None]
    assert "\n".join(page.text for page in pages) == full.text

    _assert_all_marks_invalidated(full.text)


def test_recall_screen_missing_frame_raises_named_range(tmp_path):
    session = _session()
    archive = _attach(session, tmp_path / "obs")
    session.observe()

    with pytest.raises(ObsArchiveError) as excinfo:
        archive.page(99)
    assert "screen#99" in str(excinfo.value)
    assert "screen#1" in str(excinfo.value)


def test_recall_screen_out_of_range_offset_returns_honest_receipt(tmp_path):
    session = _session()
    archive = _attach(session, tmp_path / "obs")
    session.observe()

    receipt = _tool_by_name(session, archive, "recall_screen").invoke(
        {"screen_seq": 1, "offset": 99}
    )

    assert "超出范围" in receipt
    assert "offset=0" in receipt


def test_invalidate_marks_is_idempotent_and_wraps_locates():
    once = invalidate_marks("ax_3@e9 | la_1#2@e11")
    assert once == "历史:ax_3@e9（已失效） | 历史:la_1#2@e11（已失效）"
    assert invalidate_marks(once) == once


# --------------------------------------------------------------------------
# tool receipts (read-only contract, honest failures)
# --------------------------------------------------------------------------
def _tool_by_name(session, archive, name: str):
    for tool in make_obs_archive_tools(session, archive):
        if tool.name == name:
            return tool
    raise AssertionError(f"tool {name!r} not built")


def test_recall_tools_expose_intent_and_note_and_do_not_touch_the_device(tmp_path):
    session = _session()
    archive = _attach(session, tmp_path / "obs")
    session.observe()
    calls_before = session.device_factory.screenshot_calls

    recall = _tool_by_name(session, archive, "recall_screen")
    search = _tool_by_name(session, archive, "search_screens")
    for tool in (recall, search):
        assert "intent" in tool.args and "note" in tool.args

    page_receipt = recall.invoke({"screen_seq": 1, "intent": "回看"})
    search_receipt = search.invoke({"query": "WLAN", "intent": "找帧"})

    assert "[RECALL] screen#1" in page_receipt
    assert "只读" in page_receipt and "不能用于" in page_receipt
    assert "read_screen" in page_receipt
    _assert_all_marks_invalidated(page_receipt)
    assert "[RECALL-SEARCH]" in search_receipt
    assert "screen#1" in search_receipt
    _assert_all_marks_invalidated(search_receipt)
    assert session.device_factory.screenshot_calls == calls_before


def test_tool_missing_frame_and_empty_search_return_honest_text(tmp_path):
    archive = ObsArchive(tmp_path / "obs", "run1")
    session = _session()
    session.obs_archive_sink = archive

    missing = _tool_by_name(session, archive, "recall_screen").invoke(
        {"screen_seq": 7}
    )
    assert "error: recall_screen" in missing
    assert "未找到 screen#7" in missing

    empty = _tool_by_name(session, archive, "search_screens").invoke({"query": "x"})
    assert "没有匹配帧" in empty


def test_tool_archive_failure_returns_error_text_not_an_exception(tmp_path):
    class BrokenArchive:
        def page(self, *_args, **_kwargs):
            raise RuntimeError("db locked")

        def search(self, *_args, **_kwargs):
            raise RuntimeError("db locked")

    session = _session()
    recall = _tool_by_name(session, BrokenArchive(), "recall_screen")
    receipt = recall.invoke({"screen_seq": 1})
    assert receipt.startswith("error: recall_screen")
    assert "db locked" in receipt


# --------------------------------------------------------------------------
# fail-open write path
# --------------------------------------------------------------------------
def test_raising_sink_never_breaks_observation(tmp_path):
    class ExplodingSink:
        def on_committed_observation(self, *_args, **_kwargs):
            raise RuntimeError("disk on fire")

    session = _session()
    session.obs_archive_sink = ExplodingSink()

    observation = session.observe()
    assert observation.screen_seq == 1
    assert session.marks  # the batch committed normally


def test_unwritable_archive_root_is_fail_open_and_trace_only(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    session = _session()
    session.obs_archive_sink = ObsArchive(blocker / "obs", "run1")

    observation = session.observe()

    assert observation.screen_seq == 1
    assert [event for event, _ in session.trace_events] == ["obs_archive_error"]
    assert session.trace_events[0][1] == {"error": "NotADirectoryError"}


# --------------------------------------------------------------------------
# capability off = byte-identical behavior; on = tools + hint + sink
# --------------------------------------------------------------------------
def _obs_messages() -> list:
    from langchain_core.messages import HumanMessage

    return [
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": f"[OBS] app=app screen#{i}\nmarks (3): ax_{i}|Button|t|(0,0)",
                }
            ]
        )
        for i in range(1, 4)
    ]


def _fold_text(message) -> str:
    return message.content[0]["text"]


def test_default_fold_placeholder_is_byte_identical_and_hinted_when_installed():
    messages = _obs_messages()
    pruner = ContextPrunerService(keep_images=1, keep_marks=1)
    assert pruner.fold_hint == ""
    pruner.prune(messages)

    assert _fold_text(messages[0]) == "[OBS] app=app screen#1 [marks 已折叠:3]"
    # Idempotent: a second pass leaves the folded line unchanged (no marker).
    assert pruner.prune(messages) == []

    hinted = _obs_messages()
    hinted_pruner = ContextPrunerService(keep_images=1, keep_marks=1)
    hinted_pruner.fold_hint = RECALL_HINT
    hinted_pruner.prune(hinted)
    assert (
        _fold_text(hinted[0])
        == f"[OBS] app=app screen#1 [marks 已折叠:3] {RECALL_HINT}"
    )
    assert hinted_pruner.prune(hinted) == []


def _mount_config(root: Path, mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        taskdoc_enabled=False,
        safety_mode="off",
        compact_enabled=False,
        finish_verify="off",
        deliverable_enabled=False,
        app_kb_enabled=False,
        dream_mode="off",
        experience_enabled=False,
        memory_rag="off",
        obs_archive=mode,
        obs_archive_dir=str(root),
        obs_archive_keep_runs=20,
    )


def _mount_context(config, session, pruner, factory):
    return CapabilityAssemblyContext(
        {
            "config": config,
            "session": session,
            "context_pruner": pruner,
            "obs_archive_factory": factory,
        }
    )


def test_capability_off_mounts_nothing_and_leaves_no_residue(tmp_path):
    config = _mount_config(tmp_path / "obs", "off")
    session = _session()
    pruner = ContextPrunerService(keep_images=1, keep_marks=1)
    archive = ObsArchive(tmp_path / "obs", "run1")
    ctx = assemble_capabilities(
        build_capability_registry(config),
        _mount_context(config, session, pruner, lambda: archive),
    )

    states = {row["cap_id"]: row["state"] for row in build_capability_registry(config).status()}
    assert states["obs_archive"] == "off"
    assert [tool.name for tool in ctx.tools] == []
    assert session.obs_archive_sink is None
    assert pruner.fold_hint == ""
    assert not (tmp_path / "obs").exists()


def test_capability_on_mounts_tools_hint_sink_and_releases_cleanly(tmp_path):
    config = _mount_config(tmp_path / "obs", "on")
    session = _session()
    pruner = ContextPrunerService(keep_images=1, keep_marks=1)
    archive = ObsArchive(tmp_path / "obs", "run1")
    ctx = assemble_capabilities(
        build_capability_registry(config),
        _mount_context(config, session, pruner, lambda: archive),
    )

    states = {row["cap_id"]: row["state"] for row in build_capability_registry(config).status()}
    assert states["obs_archive"] == "active"
    assert {tool.name for tool in ctx.tools} == {"recall_screen", "search_screens"}
    assert session.obs_archive_sink is archive
    assert pruner.fold_hint == RECALL_HINT

    off = CapabilityRegistry()
    for spec in build_capability_registry(config).specs():
        off.register(
            replace(spec, mode="off") if spec.cap_id == "obs_archive" else spec
        )
    assemble_capabilities(off, ctx)

    assert [tool.name for tool in ctx.tools] == []
    assert session.obs_archive_sink is None
    assert pruner.fold_hint == ""


def test_capability_build_failure_is_fail_open(tmp_path):
    config = _mount_config(tmp_path / "obs", "on")

    def exploding_factory():
        raise RuntimeError("no archive for you")

    session = _session()
    pruner = ContextPrunerService(keep_images=1, keep_marks=1)
    ctx = assemble_capabilities(
        build_capability_registry(config),
        _mount_context(config, session, pruner, exploding_factory),
    )

    assert [tool.name for tool in ctx.tools] == []
    assert session.obs_archive_sink is None
    assert pruner.fold_hint == ""


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------
def test_retention_keeps_newest_runs_and_removes_sidecars(tmp_path):
    root = tmp_path / "obs"
    root.mkdir()
    for index, name in enumerate(("old", "middle", "recent")):
        (root / f"{name}.jsonl").write_text("", encoding="utf-8")
        os.utime(root / f"{name}.jsonl", (index + 1, index + 1))
        (root / f"{name}.db").write_text("", encoding="utf-8")
        (root / f"{name}.db-wal").write_text("", encoding="utf-8")
    current = root / "run-current.jsonl"
    current.write_text("", encoding="utf-8")
    os.utime(current, (10, 10))

    removed = prune_obs_archive_runs(root, keep=2, current_run_id="run-current")

    assert removed == 2
    assert sorted(path.name for path in root.iterdir()) == [
        "recent.db",
        "recent.db-wal",
        "recent.jsonl",
        "run-current.jsonl",
    ]


def test_archive_enforces_retention_on_first_write(tmp_path):
    root = tmp_path / "obs"
    root.mkdir()
    old = root / "old.jsonl"
    old.write_text("", encoding="utf-8")
    os.utime(old, (1, 1))

    session = _session()
    archive = ObsArchive(root, "run-new", keep_runs=1)
    session.obs_archive_sink = archive
    session.observe()

    assert not old.exists()
    assert archive.jsonl_path.exists()
    assert len(_archive_records(archive)) == 1
