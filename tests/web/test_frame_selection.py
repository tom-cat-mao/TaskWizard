"""Main-frame selection logic: follow latest unless a frame is pinned."""

from __future__ import annotations

from phone_agent.web.app import _choose_frame, _frame_key, _pin_toggle


def _screens(*seqs: int) -> list[dict]:
    return [{"seq": n, "app": "设置", "image": f"data:image/png;base64,f{n}"} for n in seqs]


def _ref(ref: str) -> dict:
    return {"seq": None, "reference": True, "screen_ref": ref, "image": f"data:image/png;base64,r{ref}"}


def test_follows_latest_by_default():
    selected = {"key": None, "pinned": False}
    shown = _choose_frame(_screens(1, 2), selected)
    assert shown["seq"] == 2
    # A newer frame arrives: still following.
    shown = _choose_frame(_screens(1, 2, 3), selected)
    assert shown["seq"] == 3


def test_pin_holds_and_toggle_releases():
    selected = {"key": None, "pinned": False}
    screens = _screens(1, 2, 3)
    _choose_frame(screens, selected)
    _pin_toggle(selected, _frame_key(screens[0]))
    shown = _choose_frame(screens, selected)
    assert shown["seq"] == 1  # pinned on the historical frame
    _pin_toggle(selected, _frame_key(screens[0]))  # click again -> release
    shown = _choose_frame(screens, selected)
    assert shown["seq"] == 3  # following latest again


def test_pinned_frame_rolling_off_releases_pin():
    selected = {"key": "seq:1", "pinned": True}
    shown = _choose_frame(_screens(2, 3), selected)
    assert selected["pinned"] is False
    assert shown["seq"] == 3


def test_empty_history():
    selected = {"key": None, "pinned": False}
    assert _choose_frame([], selected) is None


def test_reference_frames_keep_independent_identity():
    # Multiple unverified reference frames all have seq=None but must not collapse.
    ref_a = _ref("abc")
    ref_b = _ref("def")
    assert _frame_key(ref_a) != _frame_key(ref_b)
    assert _frame_key(ref_a) == "ref:abc"

    screens = [{"seq": 1, "app": "设置", "image": "data:image/png;base64,f1"}, ref_a, ref_b]
    selected = {"key": None, "pinned": False}
    # Following latest -> the second reference frame.
    shown = _choose_frame(screens, selected)
    assert shown is ref_b

    # Pin the first reference frame; it holds even though both share seq=None.
    _pin_toggle(selected, _frame_key(ref_a))
    shown = _choose_frame(screens, selected)
    assert shown is ref_a

    # Pin the verified observation; distinct key resolves to it.
    _pin_toggle(selected, "seq:1")
    shown = _choose_frame(screens, selected)
    assert shown["seq"] == 1
