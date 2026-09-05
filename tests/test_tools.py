import asyncio

import pytest

from repopilot.tools.base import ToolRegistry, ToolResult, ToolSpec


async def test_list_files(registry, workspace):
    result = await registry.call("list_files", workspace, glob="**/*.py")
    assert result.ok
    assert "calculator.py" in result.content


async def test_read_file(registry, workspace):
    result = await registry.call("read_file", workspace, path="calculator.py")
    assert result.ok
    assert "def divide" in result.content
    assert result.content.startswith("   1 | ")  # line numbers added


async def test_read_missing_file_is_not_an_exception(registry, workspace):
    result = await registry.call("read_file", workspace, path="nope.py")
    assert not result.ok
    assert "not a file" in result.error


async def test_path_escape_is_converted_to_tool_error(registry, workspace):
    result = await registry.call("read_file", workspace, path="../../../etc/passwd")
    assert not result.ok
    assert "blocked by sandbox" in result.error


async def test_write_then_read_roundtrip(registry, workspace):
    written = await registry.call(
        "write_file", workspace, path="notes/hello.py", content="X = 1\n"
    )
    assert written.ok
    back = await registry.call("read_file", workspace, path="notes/hello.py")
    assert "X = 1" in back.content


async def test_unknown_tool(registry, workspace):
    result = await registry.call("rm_rf", workspace)
    assert not result.ok
    assert "unknown tool" in result.error


async def test_tool_timeout_is_enforced():
    async def slow(workspace, **_):
        await asyncio.sleep(5)
        return ToolResult(tool="slow", ok=True)

    reg = ToolRegistry(timeout=0.05)
    reg.register(ToolSpec(name="slow", description="", risk="read", fn=slow))

    result = await reg.call("slow", None)
    assert not result.ok
    assert "timed out" in result.error


async def test_semaphore_caps_concurrency():
    live = 0
    peak = 0

    async def counted(workspace, **_):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return ToolResult(tool="counted", ok=True)

    reg = ToolRegistry(timeout=5, max_concurrency=2)
    reg.register(ToolSpec(name="counted", description="", risk="read", fn=counted))

    await asyncio.gather(*(reg.call("counted", None) for _ in range(10)))
    assert peak <= 2


async def test_git_diff_reports_agent_changes(registry, workspace):
    before = await registry.call("git_diff", workspace)
    assert before.meta["empty"] is True

    await registry.call("write_file", workspace, path="calculator.py", content="# wiped\n")
    after = await registry.call("git_diff", workspace)
    assert after.ok
    assert "calculator.py" in after.content
    assert "# wiped" in after.content


@pytest.mark.slow
async def test_run_tests_detects_the_seeded_failure(registry, workspace):
    result = await registry.call("run_tests", workspace, timeout=120)
    assert result.ok  # the tool ran
    assert result.meta["passed"] is False  # but the suite is red
    assert result.meta["counts"].get("failed") == 1
