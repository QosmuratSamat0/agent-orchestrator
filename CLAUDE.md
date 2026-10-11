# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local Python 3.11 orchestrator (stdlib only, no runtime dependencies) that hands a coding task to **Antigravity** (`agy`) through the **Agent-Bridge** MCP server, waits for it to finish without polling an LLM, then runs **Codex** (`codex exec`) as an automated verifier and loops on fixable feedback. User-facing strings, log messages, and prompts are mostly in Russian; keep new ones in Russian to match.

## Commands

Windows / PowerShell. The README uses the full interpreter path `C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe`; `python` works if that interpreter is first on PATH.

```powershell
python -m pip install -e . ; python -m pip install pytest     # dev install
python -m pytest -v                                           # full suite (~1 min)
python -m pytest tests/test_orchestrator.py -v                # one file
python -m pytest tests/test_waiter.py::test_name -v           # one test
python -m agent_orchestrator.cli models                       # also: run, submit, status, list, recover, cancel, report
python -m agent_orchestrator.cli run -t "title" -p "prompt" -a "acceptance criteria" [-m model] [-e effort] [-d cwd]
```

`pyproject.toml` sets `pythonpath = ["src"]`, so tests run without an install. There is no linter or formatter configured.

## Architecture

`Orchestrator` (`orchestrator.py`) owns the lifecycle and gets every collaborator through constructor injection. That is how tests swap in fakes. One `run_task` loop iteration:

1. **Dispatch** to Antigravity via `BridgeClient.dispatch_task` (`bridge_client.py`). It spawns `agent-bridge` as a subprocess and talks MCP JSON-RPC 2.0 over stdio, with reader threads and a stderr ring buffer. `call_tool` normalizes MCP responses into plain dicts from either `structuredContent` or JSON in text content, and sets `ok: False` on `isError`. The `request_id` is `{task.request_id}_{retry_count}` so dispatches stay idempotent.
2. **Wait** with `NonLLMWaiter` (`waiter.py`). It calls `wait_task` in chunks (`wait_chunk_sec`), falls back to `check_task` on errors, and enforces `total_task_timeout_sec`. Bridge status strings are mapped through alias sets (`COMPLETED_STATUSES`, `CANCELLED_STATUSES`, …), and `orchestrator.py`/`recover()` keep their own inline copies of those aliases. If you change the status vocabulary, update all of these places.
3. **Verify** with `CodexVerifier` (`codex_verifier.py`). It captures `git status` + `git diff` in the task's cwd, builds a structured prompt, and runs `codex exec --skip-git-repo-check --ephemeral -s workspace-write -C <cwd> <prompt>`. `parse_verification_output` looks for a line matching `VERDICT: PASSED|RETRY_NEEDED|FAILED` (plus `FEEDBACK` / `TESTS_STATUS` blocks). A missing verdict counts as a failure.
4. **Decide**. `PASSED` completes the task. `RETRY_NEEDED` goes to `RetryController` (`retry_controller.py`), which builds a refinement prompt and re-dispatches until success, deadline expiry (`total_task_timeout_sec`), or failure. `FAILED` stops the loop.
5. **Report**. `generate_final_report` writes `orchestrator_report_<id8>.md` into the **task's cwd**.

Supporting pieces:
- **`repository.py`**: SQLite in WAL mode with two tables, `tasks` and `task_events`. Every state change is `update_task` + `record_event`. `create_task` enforces `max_active_tasks` (default 1) by counting `queued`/`running` rows and raises `ConcurrencyLimitError`.
- **`recover()`**: finds `queued`/`running` tasks after a crash. Using the saved `bridge_task_id`, it resumes the wait, jumps straight to verification, or marks the task failed if Bridge no longer knows it.
- **`model_router.py`**: a deterministic keyword and word-count classifier (Russian and English keywords) maps the task to `TaskComplexity` (trivial…expert), then to a model through the `[models]` config. The available-model set comes from `agy models` at construction, with `VERIFIED_AGY_MODELS` as the static fallback. Unknown manual models fall back to `models.fallback`.
- **`config.py`**: `OrchestratorConfig.load()` reads `--config`, then `$ORCHESTRATOR_CONFIG`, then `./config.toml`, then `./config.example.toml`, then built-in defaults. All paths are **relative to the process cwd**. New config keys must be added to the dataclass and also wired by hand in `load()`. Note that `codex.skip_git_repo_check`/`ephemeral` are not currently read from TOML.
- **`models.py`**: `Task` dataclass with `mark_*` transition helpers and hand-written `to_dict`/`from_dict`. A new `Task` field also needs a column in the `CREATE TABLE`, the `INSERT`/`UPDATE` statements, and `_row_to_task` in `repository.py`.

## Testing notes

- `tests/conftest.py` provides `MockBridgeClient` (in-memory status map, default status `completed`) and `MockCodexVerifier` (queue verdicts with `set_next_verdict`). Use these instead of real binaries. `test_bridge_client.py` covers the real stdio client.
- The `ModelRouter()` fixture still tries to run the real `agy models` (15 s timeout) and falls back quietly if that fails.
- Tests that call `create_task` without `cwd` use `os.getcwd()`, which is why `orchestrator_report_*.md` files pile up in the repo root after a test run. They are test artifacts and safe to delete. Pass `cwd=tmp_path` in new tests.
- Executable paths (`codex.exe`, `agy.exe`) are hard-coded machine-specific defaults in `config.py`/`model_router.py`, and `config.toml` overrides them.
