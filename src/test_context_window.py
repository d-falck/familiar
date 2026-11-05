"""Tests for transcript windowing and the fast-path effort allowlist.

The fast-path tests matter more than they look: a false positive there sends
a real instruction ("go for it", "yes") to a low reasoning budget, which is
how an agent quietly does a worse job of a deploy. The negative cases are the
point of the file.
"""

from __future__ import annotations

import context_window
import fast_path


def _msgs(n: int, size: int = 100, role_cycle: bool = True) -> list[dict]:
    out = []
    for i in range(n):
        role = "user" if (not role_cycle or i % 2 == 0) else "assistant"
        out.append({"role": role, "content": f"{i:04d}" + "x" * (size - 4)})
    return out


def test_under_budget_is_untouched():
    msgs = _msgs(10)
    assert context_window.trim(msgs, budget_chars=10_000) == msgs


def test_empty_is_untouched():
    assert context_window.trim([]) == []


def test_trims_to_newest_within_budget():
    msgs = _msgs(100, size=100)  # 10_000 chars
    out = context_window.trim(msgs, budget_chars=1_000, floor=1)
    assert out[0]["content"].startswith("[Context note:")
    kept = out[1:]
    assert context_window.total_chars(kept) <= 1_000
    # newest kept, oldest dropped
    assert kept[-1] == msgs[-1]
    assert msgs[0] not in kept


def test_floor_beats_budget():
    msgs = _msgs(50, size=1_000)
    out = context_window.trim(msgs, budget_chars=10, floor=5)
    assert len(out) == 6  # marker + 5
    assert out[1:] == msgs[-5:]


def test_floor_larger_than_history_keeps_everything():
    msgs = _msgs(3, size=5_000)
    assert context_window.trim(msgs, budget_chars=1, floor=99) == msgs


def test_marker_can_be_suppressed():
    msgs = _msgs(40, size=100)
    out = context_window.trim(msgs, budget_chars=500, floor=1, marker=False)
    assert all("[Context note:" not in m["content"] for m in out)


def test_marker_is_a_user_turn_so_the_transcript_still_opens_with_one():
    msgs = _msgs(40, size=100)
    out = context_window.trim(msgs, budget_chars=500, floor=1)
    assert out[0]["role"] == "user"


def test_marker_reports_the_dropped_count():
    msgs = _msgs(40, size=100)
    out = context_window.trim(msgs, budget_chars=500, floor=1)
    dropped = 40 - (len(out) - 1)
    assert f"{dropped} older message(s)" in out[0]["content"]


def test_budgets_are_ordered_sanely():
    assert (
        context_window.fast_budget()
        < context_window.background_budget()
        < context_window.session_budget()
    )


# --- fast path ---------------------------------------------------------


def test_chitchat_is_fast():
    for text in ["hi", "Damon: hey", "thanks!", "you there?", "radio check", "COOL"]:
        assert fast_path.is_chitchat([{"role": "user", "content": text}]), text


def test_instructions_are_never_fast():
    # These are the shortest messages in the real transcript and every one of
    # them authorises work. A regression here is silently worse judgement.
    for text in [
        "yes",
        "ok",
        "okay",
        "sure",
        "go",
        "go for it",
        "go on then",
        "do it",
        "continue",
        "try again",
        "hi can you check my email",
        "deploy",
        "yep",
        "fine",
        "hello? can you fix the bug",
    ]:
        assert not fast_path.is_chitchat([{"role": "user", "content": text}]), text


def test_multiline_is_never_fast():
    assert not fast_path.is_chitchat(
        [{"role": "user", "content": "hi\nalso please archive everything"}]
    )


def test_scheduler_prompt_is_never_fast():
    assert not fast_path.is_chitchat(
        [{"role": "user", "content": "[scheduled check-in @ Wed] Non-triage tick."}]
    )


def test_assistant_last_is_never_fast():
    assert not fast_path.is_chitchat([{"role": "assistant", "content": "hi"}])


def test_empty_is_never_fast():
    assert not fast_path.is_chitchat([])


def test_effort_for_picks_budgets():
    assert fast_path.effort_for([{"role": "user", "content": "hi"}]) == fast_path.FAST_EFFORT
    assert (
        fast_path.effort_for([{"role": "user", "content": "go for it"}])
        == fast_path.NORMAL_EFFORT
    )
