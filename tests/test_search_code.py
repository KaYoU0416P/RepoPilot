"""Contract for the search_code tool.

These tests are RED until you implement search_code in
src/repopilot/tools/fs_tools.py. That is today's handwrite task.

    uv run pytest tests/test_search_code.py -v
"""


async def test_finds_a_function_definition(registry, workspace):
    result = await registry.call("search_code", workspace, pattern=r"def divide")
    assert result.ok, result.error
    assert "calculator.py:" in result.content
    assert result.meta["count"] >= 1


async def test_result_lines_are_path_line_text(registry, workspace):
    result = await registry.call("search_code", workspace, pattern=r"def add")
    line = result.content.splitlines()[0]
    path, lineno, text = line.split(":", 2)
    assert path == "calculator.py"
    assert lineno.isdigit()
    assert "def add" in text


async def test_no_matches_is_still_ok(registry, workspace):
    result = await registry.call("search_code", workspace, pattern=r"zzz_not_here")
    assert result.ok
    assert result.meta["count"] == 0
    assert result.content == "(no matches)"


async def test_invalid_regex_is_a_tool_error_not_a_crash(registry, workspace):
    result = await registry.call("search_code", workspace, pattern=r"def (")
    assert not result.ok
    assert result.error


async def test_max_results_is_respected(registry, workspace):
    result = await registry.call("search_code", workspace, pattern=r".", max_results=3)
    assert result.ok
    assert result.meta["count"] == 3
    assert len(result.content.splitlines()) == 3


async def test_glob_filters_files(registry, workspace):
    await registry.call("write_file", workspace, path="notes.txt", content="def divide\n")
    py_only = await registry.call("search_code", workspace, pattern=r"def divide", glob="**/*.py")
    assert "notes.txt" not in py_only.content

    txt_only = await registry.call(
        "search_code", workspace, pattern=r"def divide", glob="**/*.txt"
    )
    assert "notes.txt:1:" in txt_only.content
