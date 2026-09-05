import pytest

from repopilot.workspace import PathEscapeError


def test_workspace_is_a_copy(workspace, settings):
    assert workspace.root != settings.sample_repo
    assert (workspace.root / "calculator.py").is_file()


def test_resolve_allows_paths_inside(workspace):
    assert workspace.resolve("calculator.py").is_file()


@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "/etc/passwd", "sub/../../../outside.py"],
)
def test_resolve_blocks_escape(workspace, bad):
    with pytest.raises(PathEscapeError):
        workspace.resolve(bad)
