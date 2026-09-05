# Real failures hit while building

## 1. macOS `UF_HIDDEN` silently breaks editable installs

**Symptom.** After `uv sync`, `uv run pytest` died with
`ModuleNotFoundError: No module named 'repopilot'`, even though `uv pip list` showed
`repopilot 0.1.0 /Users/kayou/Documents/py_agent` and
`site-packages/_editable_impl_repopilot.pth` existed with the correct contents.

**Cause.** On this machine every file `uv` writes gets the macOS `UF_HIDDEN` file flag
(confirmed with `ls -lO`; even `.venv/bin/python` has it). CPython's
`site.addpackage()` explicitly skips `.pth` files carrying `UF_HIDDEN` and returns
**silently** — no warning. So neither `_editable_impl_repopilot.pth` nor
`_virtualenv.pth` was ever processed, and `src/` never reached `sys.path`.
Confirmed by `'_virtualenv' in sys.modules` being `False` at startup.

**Fix.** Two layers, because `uv sync` re-applies the flag every time:
- `make sync` runs `chflags nohidden .venv/lib/python*/site-packages/*.pth` after syncing.
- `pyproject.toml` sets `[tool.pytest.ini_options] pythonpath = ["src"]` so the test
  suite works even if the `.pth` is skipped.

**Test added?** No — it is environmental, not a code defect. Documented here instead.

**Lesson.** "Package is installed" and "package is importable" are different claims.
When an import fails despite a correct-looking install, check whether `.pth` processing
ran at all (`'_virtualenv' in sys.modules`) before suspecting your own packaging.

---

## 2. The scripted LLM double parsed prompt text as file content

**Symptom.** The full loop ran all six nodes and produced a diff, but the tests stayed
red for all three attempts and `calculator.py` ended up containing the sentence
`Return the COMPLETE new content for every file you change.`

**Cause.** `execute` embedded file contents as `### <path>` headings, and the prompt
template put its closing instructions *after* the file section. `ScriptedLLM._sections`
consumed every line after a heading until the next heading, so the trailing instructions
were appended to the file body and written to disk. Invalid Python, so pytest failed.

A second bug in the same area: `_python_sources` picked file paths by "line ends in
`.py`", which matched the heading line `### calculator.py` itself. `plan.files_to_edit`
became `["### calculator.py"]`, every `read_file` failed, and `execute` wrote 0 files.

**Fix.** File contents are now emitted inside explicit ``` fences and the parser reads
only between the fences. `_python_sources` skips any line containing whitespace.

**Test added?** Yes — `tests/test_graph.py::test_full_loop_fixes_the_bug` asserts
`files_changed == ["calculator.py"]` and `"raise ValueError" in state["diff"]`, which
both fail under either bug.

**Lesson.** Delimiter-free prompt sections are a parsing bug waiting to happen. This is
the same class of problem as SQL injection: data and instructions sharing a channel with
no escaping. Fence anything the model has to read back verbatim.
