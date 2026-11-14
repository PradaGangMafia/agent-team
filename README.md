# Agent-Team

Two agents, one workflow. A **MANAGER** works through a queue of prompt files in
`tasks/`, hands each one to a **WORKER** agent, waits for it to finish, verifies
that the work really happened — log in `logs/`, artefacts in `work/` — and only
then moves to the next task.

Built for exactly the use cases that break single-agent scripts:

- **Image batches from master images** — one reference artwork in `ref/`
  becomes ten on-model variations through `/v1/images/edits`.
- **Long coding tasks split into prompts** — `p01` reviews, `p02` refactors,
  `p03` tests; each task only runs once the previous one is verified.

Pure Python 3.9+ standard library. No dependencies, no build step, no framework.

```
tasks/P01.md ──▶ MANAGER ──▶ WORKER ──▶ work/ + logs/p01.log
                    ▲                        │
                    └────── verify ◀─────────┘
                              │ pass
                              ▼
                         tasks/P02.md …
```

## Quick start

```bash
cp example.env .env          # add your endpoint + keys
python agent_team.py doctor  # checks config, folders, prompts, endpoints
python agent_team.py run     # executes the queue in order
```

No keys yet? Test the whole pipeline offline:

```bash
python agent_team.py run --simulate
```

`--simulate` never touches the network: the mock worker writes placeholder
files where the real worker would put artwork or code, so you can watch the
delegation, logging and verification flow end to end. Delete those placeholders
(`git clean -fd work logs` handles the ignored ones) before a real run.

## Two endpoints, two personalities

Each agent has its own base URL, API key, model and sampling settings, so the
manager can be a cheap fast model while the worker is a strong vision model —
even from different providers.

```ini
MANAGER_BASE_URL=https://api.openai.com/v1
MANAGER_API_KEY=sk-...
MANAGER_MODEL=gpt-4o-mini
MANAGER_TEMPERATURE=0.2

WORKER_BASE_URL=https://openrouter.ai/api/v1
WORKER_API_KEY=sk-or-...
WORKER_MODEL=anthropic/claude-sonnet-4
WORKER_MAX_TOKENS=8192
```

Any OpenAI-compatible `/v1` endpoint works: OpenAI, OpenRouter, LiteLLM,
together.ai, vLLM, Ollama (`http://localhost:11434/v1`), LocalAI, llama.cpp
server, LM Studio, Azure-style proxies… See `example.env` for every option
(timeouts, `*_EXTRA_JSON`, round limits, retries, log size floor, folder paths).

## The workflow

1. `run` discovers `tasks/*.md`, sorts them naturally (`p1, p2, p10`) and takes
   them in order. `--tasks`, `--start` and `--max-tasks` narrow the queue.
2. For each task the **manager** gets the task text plus queue context and a
   toolset: `list_dir`, `read_file`, `read_image`, `send_task_to_worker`,
   `finish_task`.
3. `send_task_to_worker` runs a **worker** session that can
   `list_dir`, `read_file`, `write_file`, `read_image`, `generate_image`,
   `run_python`, `run_shell`, `write_log`, `append_log` and finally
   `finish_task`. The worker must write `logs/<id>.log` and put its output in
   `work/` before it may finish.
4. The manager is told the worker finalised, then inspects the log and `work/`
   itself and returns a verdict: `pass`, `retry` (with actionable notes),
   `skip` or `halt`.
5. **The verdict is not the only gate.** Deterministic checks run after every
   attempt:
   - `logs/<id>.log` exists and is at least `MIN_LOG_BYTES` (default 120) of
     real content;
   - `work/` contains new or changed files (unless `REQUIRE_WORK_OUTPUT=false`);
   - every `expects:` path from the task front matter exists.
   A failed gate forces a retry even if the manager said `pass`.
6. On success the next task starts in a fresh manager session that sees what
   has already been completed. On failure the workflow halts by default
   (`CONTINUE_ON_FAILURE=true` to push on) and the run exits non-zero.

Every run also writes a timestamped log to `logs/run-*.log`, with a colourless
event trail of every manager and worker decision.

## Writing tasks

`tasks/P01.md`:

```markdown
---
id: p01
title: Generate five variations from the master image
expects:
  - work/images/*.png
---

# Generate five variations from the master image

`ref/master_image.png` is the approved master artwork …
```

- **File name** sets the order; the **id** (front matter, else file stem,
  lowercased) names the log: `p01` → `logs/p01.log`. Keep one log per task.
- `expects:` is optional; it pins required deliverables (glob patterns,
  relative to the project root). Without it, any new/changed file in `work/`
  passes the gate.
- Everything else is free-form instruction text — that is the prompt the worker
  sees. Be explicit about deliverables, constraints and acceptance criteria.
- `work: false` in front matter disables the "must produce work output" rule
  for a pure-analysis task that is meant to write only a log.

Scaffold the next one with `python agent_team.py new-task --title "…"`.

### Splitting a long coding job

Write the queue as a sequence of dependent prompts, one per file, and let each
finish before the next starts:

```
p01  review ref/master.py         → work/master_review.md
p02  refactor + tests from p01     → work/master_refactored.py, tests
p03  generate docs/usage examples  → work/docs/*.md
```

The worker sees the whole queue in its context and reads previous deliverables
from `work/`, so later prompts can build on earlier results.

### Generating many images from master images

Put the approved artwork in `ref/` and ask for concrete counts and names. The
worker studies the master with `read_image`, then calls `generate_image` with
`references: ["ref/master.png"]`, which routes to `/v1/images/edits` (multiple
masters are supported). `count` up to 8 per call, or several calls with
per-variant prompts.

Image backend selection (`IMAGE_MODE`):

| mode            | behaviour                                                                 |
| --------------- | ------------------------------------------------------------------------- |
| `auto` (default)| `/v1/images/edits` for referenced images, `/v1/images/generations` for text-only, falls back to multimodal chat if the images API rejects the request |
| `images`        | images API only                                                            |
| `chat`          | multimodal `/v1/chat/completions` only (OpenRouter, Gemini-style endpoints) |

Sizes, quality, the multipart field name (`image` vs `image[]` on some servers)
and the response format are all configurable in `.env`. Generated files land in
`work/` — the tool never writes to `ref/`.

## CLI

| command | what it does |
| --- | --- |
| `run` | execute the queue (`--tasks`, `--start`, `--max-tasks`, `--attempts`, `--simulate`) |
| `doctor` | validate folders, prompts, endpoints, keys; `--ping` calls `/v1/models` |
| `status` | show tasks, log sizes, expected deliverables and current `work/` contents |
| `new-task` | scaffold the next task file |
| `init` | create folders and copy `example.env` → `.env` |
| `selftest` | offline end-to-end test of the manager → worker → verify pipeline |

Global flags: `--root <dir>`, `--verbose`, `--quiet`, `--version`.

## Folder layout

```
agent_team.py          the runner (single file, stdlib only)
manager.md             manager agent system prompt
worker.md              worker agent system prompt
example.env            all configuration, documented
.env                   your keys (git-ignored)
tasks/                 p01.md, p02.md, p03.md … the queue
ref/                   reference inputs: master images, source, briefs
logs/                  p01.log, p02.log … + run-*.log
work/                  everything the agents produce
```

## Tests

```bash
python -m unittest discover -s tests -v   # full suite, offline
python agent_team.py selftest             # full pipeline in a temp project
```

`tests/test_http_integration.py` starts a local mock OpenAI-compatible server
and drives a complete run through it, covering the real HTTP client, tool
calling, multipart `/v1/images/edits` with a master image, base64 saving and the
multimodal chat image fallback — all without keys or internet access.

The shipped example queue is real work, not fixtures: `ref/master.py` contains
deliberate defects for P01/P02 to find and fix, and `ref/master_image.png` is a
master artwork for P03's image batch.

## Safety

- **Path sandbox:** every tool path is resolved against the project root;
  escaping paths are rejected. Writes are only allowed inside `work/` and
  `logs/` — `ref/`, `tasks/` and the prompts are effectively read-only to the
  agents.
- **Shell:** `run_shell` and `run_python` execute *as you*, with `cwd=work/`.
  Set `ALLOW_SHELL=false` for analysis/image-only workflows.
- **Cost:** each task can use dozens of model calls. `WORKER_MAX_ROUNDS` and
  `MANAGER_MAX_ROUNDS` bound each attempt; `MAX_TASK_ATTEMPTS` bounds retries.
- **Keys** live in `.env` (git-ignored) or the environment. They are never
  printed unmasked.

## Troubleshooting

- `IMAGE_MODEL is not set` — set it, or use `IMAGE_MODE=chat` with a
  multimodal model that returns images.
- Images API returns `HTTP 400/404` — the endpoint may not implement
  `/images/edits`; the `auto` mode falls back to chat. Set
  `IMAGE_EDIT_FIELD=image[]` for servers that expect the array notation.
- Task keeps failing verification — read `logs/<id>.log` and the run log; the
  gate's reasons are printed. Helpful fixes: lower `MIN_LOG_BYTES` for terse
  tasks, add `expects:` patterns, or steer the prompt with explicit file names.
- Worker never finalises — the task is probably too large for one attempt; split
  it into more files in `tasks/` and raise `WORKER_MAX_ROUNDS`.
- Model ignores tools — pick a tool-calling capable model for the worker;
  `--verbose` shows every call and its result.

## Extending

The script is deliberately one readable file. The natural extension points:

- `TOOL_SCHEMAS` / `WorkerAgent.dispatch` — add a tool (web fetch, database,
  renderer…).
- `verify_task` — tighten the gate (schema validation, image dimensions,
  grep for TODOs).
- `ManagerAgent` — swap the LLM manager for a pure-Python policy if you want
  zero-cost orchestration.
- `ClientFactory` — plug in your own client for non-OpenAI transports.
