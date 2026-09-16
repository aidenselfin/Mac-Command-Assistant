# Voice Action — text filesystem slice

Python 3.12 + uv and Node.js. The CLI application allowlist exposes
`list_directory`, read-only `search_files`/`read_text_file`, and `write_file`.
Delete/move tools are never accepted.

## Setup and run

```sh
uv sync --locked
npm ci
```

Set `OPENROUTER_API_KEY` in your shell, or put `OPENROUTER_API_KEY=...` in
`.voice-action/config.env` (ignored by Git). The application also supports
`~/.voice-action/config.env`. Shell variables take precedence over these files.
Do not put keys in JSON or commit them.

With an OpenRouter key, the default provider is `openrouter` and the default model
is `openai/gpt-4o-mini`. To select explicitly, set `VOICE_ACTION_PROVIDER` and
`VOICE_ACTION_MODEL`, or pass `--provider` and `--model`.

```sh
uv run python main.py --text "voice-action-test 폴더 파일 목록 보여줘"
```

The equivalent command inside the activated uv environment is:

```sh
source .venv/bin/activate
python main.py --text "voice-action-test 폴더 파일 목록 보여줘"
```

The bundled `voice-action-test` directory is the default allowed root. To list
another existing directory, use `--allowed-directory /absolute/path` (repeatable).
The planner receives each root's directory name as its alias. All relative
configuration paths resolve against the working directory; run from this repo.

Configuration priority: CLI > `VOICE_ACTION_*` environment values > local
`config.json` > `~/.voice-action/config.json` > defaults. Copy
`config.example.json` if needed; it explicitly selects OpenRouter. To use OpenAI,
change its provider/model and set `OPENAI_API_KEY`. `VOICE_ACTION_ALLOWED_DIRECTORIES` accepts
a JSON array. Older key fields in global JSON are ignored; local JSON keys are
rejected. Credentials are read only from environment values/env files.

Only one provider/model is used per command. Multi-step requests pass each MCP
result back to that same model. For repeated commands, keep the MCP process and
HTTP client alive:

```sh
uv run python main.py --interactive
```

Tools execute sequentially, at most five tool rounds. There is no automatic retry
or provider fallback. Interactive mode keeps one MCP process/session and HTTP
client for all commands. Command-level planner errors are displayed; MCP connection
failures stop the application. EOF and exit/quit close normally; Ctrl+C exits with
130. Stdin is cancellable, so shutdown does not wait for another key press.

`RuntimeController` owns command state and pending clarification for both text and
interactive modes. Active text commands move through `IDLE`, `PLANNING`, and
`EXECUTING_TOOL`; clarification adds `WAITING_USER_INPUT` and `RESUMING_PLAN`.
Terminal `DONE` and `ERROR` states are recorded before returning to `IDLE`.
Concurrent input during planning or tool execution returns `[BUSY]` without calling
the planner again. Planner tool transitions use structured events rather than log
text. `RECORDING` and `TRANSCRIBING` are declared for later voice integration only.

The planner returns typed completed, clarification, or error outcomes. It asks only
when an unresolved choice would materially change the result. An ambiguous file
request first uses `search_files`: one candidate continues automatically, multiple
candidates are displayed as numbered choices, and zero candidates end safely without
creating or modifying a file. In interactive mode, the next number (`2` or `2번`) resumes the pending task; `취소` or
`cancel` ends it without a write. At most two clarification prompts are allowed.
Vague edit criteria are clarified before a write. Destructive requests are rejected
before any model or MCP tool call.

The typed request is sent to the configured LLM. Results in a multi-step tool loop
are sent back to the same LLM as untrusted tool data. Clarification uses a virtual,
typed model contract that is never sent to the MCP server. Rich markup is disabled
for model/tool output. API exceptions are summarized without raw payloads.

## MCP client contract

Existing `MCPClient(config)`, `async with client.connect()`, `client.tools`, and
`await client.call_tool(name, arguments)` remain available. The client default remains
list-only. To opt into the CLI capabilities:

```python
client = MCPClient(
    config,
    allowed_tools={"list_directory", "search_files", "read_text_file", "write_file"},
)
async with client.connect():
    tools = await client.list_tools()
    # Call tools with absolute paths inside config.allowed_directories.
```

At startup the client resolves the allowed roots, launches the pinned Node server,
initializes the session, and discovers all tool pages at runtime. Only audited,
explicitly allowlisted tools are exposed. Their argument schemas come from the
server; missing or incompatible required capabilities fail startup. `list_tools()`
returns a defensive copy of that startup snapshot, avoiding rediscovery per command.

All calls pass schema validation and resolved-root checks, including new write
targets and symlinks. Paths returned by `search_files` are resolved and checked again
as existing files inside an allowed root before the planner can use them. The server
independently enforces the same roots. A timeout,
cancellation, or transport failure invalidates the session; calls are never replayed
and the server is not automatically restarted. Context exit closes the SDK session
and stdio transport in the owning task, allowing the SDK to reap the Node process.

`ToolResult` preserves `ok`, `text`, and `structured`, and adds `error` and `truncated`.
Text blocks are combined in order. Non-text blocks become explicit placeholders;
binary/resource payloads are not forwarded. `max_tool_result_chars` bounds text and
the serialized structured payload: if structured data cannot fit in the remaining
budget it is omitted as a whole (never returned as malformed partial JSON).
Structured-only results also get a bounded text representation. Truncation adds a
fixed `[TRUNCATED]` marker outside the payload budget and sets `truncated=True`.
The cap applies after the SDK receives the result, not to transport-level memory.
Server-reported tool failures retain `ok=False` and a bounded `error` string.

## Verification

```sh
uv run --locked python --version
uv run --locked ruff check .
uv run --locked pytest -q
uv run --locked pytest -q -m integration
uv run --locked python main.py --text "voice-action-test 폴더 파일 목록 보여줘"
```

Pytest includes real Filesystem MCP integration tests using temporary directories
and a scripted model. After `npm ci`, they need neither network nor a real API key.
Tests exercise listing, new-file writes/reads, outside-root and symlink rejection,
result truncation, session reuse, and actual process exit on errors, cancellation,
startup timeout, EOF, exit, and SIGINT. The CLI smoke command uses the real LLM and
requires a valid key and network. The MCP npm dependency is pinned in `package.json`
and `package-lock.json`; Python dependencies are locked in `uv.lock`.

Implementation references: [MCP client lifecycle](https://modelcontextprotocol.io/docs/develop/build-client)
and [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling).
