"""Board parser tests: the pinned TaskDoc block → structured sections."""

from phone_agent.web.app import _parse_board

SAMPLE = """[TASK_DOC]
## 目标
base: 打开B站客户端找到影视飓风最新黄金视频并播放
## 路线
- [completed] 1: 打开B站客户端（桌面有PiliPlus第三方客户端）（证据：首页已打开，顶部有搜索框）
- [completed] 2: 搜索"影视飓风 黄金"（证据：搜索结果第一条为官方账号视频）
- [in_progress] 3: 找到最新黄金视频并播放
- [pending] 4: 确认播放稳定
- [blocked] 5: 发弹幕（原因：未登录）

## 流程线（最近 2 步）
#13 等待确认视频持续播放 → wait4s → ok｜画面已在播放
#14 收尾任务板 → update_task_doc → 未写入（校验失败）：不允许把 pending 项直接标 completed"""


def test_parse_board_full_sections():
    parsed = _parse_board(SAMPLE)
    assert parsed["goal"].startswith("打开B站客户端")
    assert parsed["amendments"] == []
    assert [item["status"] for item in parsed["items"]] == [
        "completed",
        "completed",
        "in_progress",
        "pending",
        "blocked",
    ]
    assert parsed["items"][0]["id"] == "1"
    assert parsed["items"][0]["note"] == "首页已打开，顶部有搜索框"
    assert parsed["items"][1]["content"] == '搜索"影视飓风 黄金"'
    assert parsed["items"][2]["note"] == ""
    assert parsed["items"][4]["note"] == "未登录"
    assert len(parsed["flow"]) == 2
    assert parsed["flow"][0].startswith("#13 等待确认视频持续播放")
    assert parsed["raw"] == ""


def test_parse_board_empty_and_garbage():
    assert _parse_board("") == {
        "goal": "",
        "amendments": [],
        "items": [],
        "flow": [],
        "raw": "",
    }
    garbage = _parse_board("something unexpected\n没有小节")
    assert garbage["raw"].strip() != ""
    assert garbage["items"] == []


def test_parse_board_amendments_and_english():
    text = (
        "## Goal\nbase: open settings\nAmendments:\n- also enable WLAN\n"
        "## Plan\n- [completed] 1: open settings (evidence: settings shown)\n"
    )
    parsed = _parse_board(text)
    assert parsed["goal"] == "open settings"
    assert parsed["amendments"] == ["also enable WLAN"]
    assert parsed["items"][0]["note"] == "settings shown"
