"""Prompt text kept out of node logic so it can be tuned without touching the graph."""

SYSTEM = """You are RepoPilot, a careful software engineer working inside an isolated
copy of a repository. You may only change files in this repository. Make the smallest
change that satisfies the task and keeps the existing tests passing. Never invent files
you have not been shown."""

ANALYZE_USER = """# Task
{task}

# Repository files
{tree}

Identify which files matter for this task and what to search for.
Prefer source files over test files. List at most 5 files and 3 regex search patterns."""

PLAN_USER = """# Task
{task}

# Analysis
{reasoning}

# Evidence gathered
{evidence}

Produce a minimal plan. Only list files you have actually seen in the evidence."""

EXECUTE_USER = """# Task
{task}

# Plan
{summary}
{approach}

# Current file contents
{files}
{feedback}
Return the COMPLETE new content for every file you change. Do not use diffs or
placeholders like "... unchanged ...". Change as few files as possible."""

RETRY_FEEDBACK = """
# Previous attempt FAILED
Attempt {attempt} of {max_attempts}. The test suite reported:

```
{test_output}
```

Diagnose why the previous edit did not work and fix it. Do not repeat the same edit."""
