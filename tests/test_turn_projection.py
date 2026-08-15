from tmuxbot.core.turn_projection import ProgressIntent, TurnProjection


def test_turn_projection_compresses_tool_and_plan_updates_into_one_snapshot():
    projection = TurnProjection(update_interval_seconds=0)

    first = projection.consume(
        "tool",
        "🔎 读取文件 <code>/repo/app.py</code>\n完整工具参数不应原样铺满过程卡",
        now=10.0,
    )
    second = projection.consume(
        "plan",
        "计划更新\n1. 修复解析 pending\n2. 跑完整测试 in_progress",
        now=11.0,
    )
    third = projection.consume(
        "tool",
        "✓ 改文件成功 <code>app.py</code>",
        now=12.0,
    )

    assert first == [
        ProgressIntent(
            action="upsert",
            display_state="working",
            body_html=(
                "💭 <b>工作进度</b>\n"
                "· 当前：🔎 读取文件 /repo/app.py 完整工具参数不应原样铺满过程卡\n"
                "· 统计：工具 1"
            ),
        )
    ]
    assert len(second) == len(third) == 1
    assert "计划 1" in second[0].body_html
    assert "修复解析 pending" not in second[0].body_html
    assert "· 统计：工具 2 · 计划 1" in third[0].body_html
    assert "✓ 改文件成功 app.py" in third[0].body_html
    assert len(third[0].body_html.splitlines()) <= 5


def test_turn_projection_throttles_updates_and_flushes_latest_summary_on_finalize():
    projection = TurnProjection(update_interval_seconds=2.0)

    assert projection.consume("tool", "读取 a.py", now=10.0)
    assert projection.consume("tool", "读取 b.py", now=10.5) == []
    assert projection.consume("tool", "修改 b.py", now=11.0) == []

    final = projection.finalize(now=11.1)

    assert final == [
        ProgressIntent(
            action="finalize",
            display_state="completed",
            body_html=(
                "✅ <b>过程摘要</b>\n"
                "· 统计：工具 3\n"
                "· 最近：读取 a.py；读取 b.py；修改 b.py"
            ),
        )
    ]


def test_turn_projection_delays_first_card_and_quick_final_stays_result_only():
    projection = TurnProjection(
        progress_delay_seconds=4.0,
        update_interval_seconds=2.0,
    )

    assert projection.consume("tool", "读取 a.py", now=10.0) == []
    assert projection.next_update_in(now=12.0) == 2.0
    assert projection.progress_was_published is False
    assert projection.finalize(now=12.0)[0].action == "finalize"


def test_turn_projection_creates_delayed_card_with_latest_summary():
    projection = TurnProjection(
        progress_delay_seconds=4.0,
        update_interval_seconds=2.0,
    )

    projection.consume("tool", "读取 a.py", now=10.0)
    projection.consume("tool", "修改 a.py", now=11.0)

    assert projection.flush(now=13.9) == []
    assert projection.flush(now=14.0) == [
        ProgressIntent(
            action="upsert",
            display_state="working",
            body_html=(
                "💭 <b>工作进度</b>\n"
                "· 当前：修改 a.py\n"
                "· 统计：工具 2"
            ),
        )
    ]


def test_turn_projection_flushes_latest_throttled_update_after_interval():
    projection = TurnProjection(update_interval_seconds=2.0)

    projection.consume("tool", "读取 a.py", now=10.0)
    projection.consume("tool", "修改 a.py", now=10.5)

    assert projection.flush(now=11.9) == []
    assert projection.flush(now=12.0) == [
        ProgressIntent(
            action="upsert",
            display_state="working",
            body_html=(
                "💭 <b>工作进度</b>\n"
                "· 当前：修改 a.py\n"
                "· 统计：工具 2"
            ),
        )
    ]


def test_turn_projection_deduplicates_identical_progress_events():
    projection = TurnProjection(update_interval_seconds=0)

    assert projection.consume("tool", "运行 pytest", now=10.0)
    assert projection.consume("tool", "运行 pytest", now=11.0) == []

    final = projection.finalize(now=12.0)[0]
    assert "工具 1" in final.body_html
