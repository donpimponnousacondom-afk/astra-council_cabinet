# Shared test support

Import reusable setup, protocol fakes and task-joining helpers from `support.<domain>`.
Pytest's existing test-directory import path exposes this package; test modules must
not import other `test_*` modules. Keep the `kernel` and `owner` fixtures canonical
in `tests/conftest.py`, alongside the existing `configured` and `ingest` setup.

- `provider`, `provider_streaming`: mock HTTP clients, fragmented SSE, request arguments and captured evidence.
- `runtime`, `runtime_feedback`: completion/tool responses, turn joining and runtime setup.
- `background_jobs`, `research_assistant`, `research_fanout`, `background_handoff`: job lifecycle and research setup.
- `addressing`, `typing`, `slash_commands`, `discord_panels`, `discord_formatting`: Discord message/client/interaction fakes.
- `vision`, `console`, `web_search`, `global_memory`, `agentic_runtime`, `featherless_tester`: feature-specific shared setup and evidence helpers.

Keep helpers scoped to their domain and dependencies acyclic. A helper used by just
one test module may remain there. Direct SQL remains appropriate when a test asserts
persistence, accounting, recovery or schema integrity; do not hide those assertions
behind generic store wrappers.
