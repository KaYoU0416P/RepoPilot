"""Deterministic test double for the LLM.

NOT an agent. It has a small hardcoded rule table that only knows the bundled
fixture repo. Its purpose is to let tests and offline demos exercise the whole
graph (nodes, retries, sandbox, diff) without network calls or token spend.
Anything it "solves" proves the pipeline works, not that the model is smart.
"""

from typing import TypeVar

from pydantic import BaseModel

from repopilot.agent.schemas import Analysis, EditSet, FileEdit, Plan
from repopilot.llm.base import LLMError
from repopilot.llm.usage import Usage

T = TypeVar("T", bound=BaseModel)

# (marker that must be present in the file, text to replace, replacement)
_RULES: list[tuple[str, str, str]] = [
    (
        "def divide",
        "def divide(a: float, b: float) -> float:\n    return a / b",
        "def divide(a: float, b: float) -> float:\n"
        '    if b == 0:\n        raise ValueError("division by zero")\n'
        "    return a / b",
    ),
]


class ScriptedLLM:
    name = "scripted"
    #: 不是真模型，但要占住这个字段 —— 成本报表按 model 查定价表，
    #: 查不到就返回「未知」而不是 0，正好也验证了那条路径。
    model = "scripted"

    def __init__(self) -> None:
        self.calls: list[str] = []
        #: token 恒为 0，只有 `calls` 会涨。测试跑一整轮图，
        #: 成本必须是 0 —— 这条也是「测试不花钱」的一个断言口子。
        self.usage = Usage()

    async def structured(self, *, system: str, user: str, schema: type[T]) -> T:
        self.calls.append(schema.__name__)
        self.usage = self.usage + Usage(calls=1)

        if schema is Analysis:
            return Analysis(  # type: ignore[return-value]
                reasoning="scripted: assume the non-test python files are relevant",
                relevant_files=_python_sources(user),
                search_queries=["def divide"],
            )

        if schema is Plan:
            return Plan(  # type: ignore[return-value]
                summary="scripted: patch the source file that matches a known rule",
                approach="apply hardcoded fixture rule",
                files_to_edit=_python_sources(user),
            )

        if schema is EditSet:
            return EditSet(  # type: ignore[return-value]
                rationale="scripted: literal replacement from the rule table",
                edits=_apply_rules(user),
            )

        raise LLMError(f"ScriptedLLM has no canned answer for {schema.__name__}")


def _python_sources(prompt: str) -> list[str]:
    """Pick bare .py paths out of a prompt, ignoring headings like '### calculator.py'."""
    files = []
    for raw in prompt.splitlines():
        line = raw.strip()
        if any(ch.isspace() for ch in line):
            continue
        if line.endswith(".py") and not line.rsplit("/", 1)[-1].startswith("test_"):
            if line not in files:
                files.append(line)
    return files[:3]


def _apply_rules(prompt: str) -> list[FileEdit]:
    """Recover file contents from the '### <path>' sections the execute node embeds."""
    edits: list[FileEdit] = []
    for path, body in _sections(prompt).items():
        new_body = body
        for marker, old, new in _RULES:
            if marker in new_body and old in new_body:
                new_body = new_body.replace(old, new)
        if new_body != body:
            edits.append(
                FileEdit(path=path, content=new_body, rationale="scripted rule applied")
            )
    return edits


def _sections(prompt: str) -> dict[str, str]:
    """Parse '### <path>' followed by a ``` fenced block into {path: content}."""
    sections: dict[str, str] = {}
    lines = prompt.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("### ") and i + 1 < len(lines) and lines[i + 1].startswith("```"):
            path = lines[i][4:].strip()
            body: list[str] = []
            i += 2
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            sections[path] = "\n".join(body).strip() + "\n"
        i += 1
    return sections
