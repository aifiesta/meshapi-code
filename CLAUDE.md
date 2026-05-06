# meshapi-code — Claude Context

Terminal chat REPL for [Mesh API](https://meshapi.ai), the OpenAI-compatible LLM gateway. Modeled on Claude Code and Aider.

PyPI package = `meshapi-code`. Command on `$PATH` = `meshapi` (same split Claude Code uses: package `@anthropic-ai/claude-code`, command `claude`).

## Commands

```bash
pipx install -e .       # local dev install (or: uv tool install -e .)
meshapi                 # launch REPL
meshapi --version
python -m build         # build wheel + sdist for PyPI
twine check dist/*      # validate before upload
```

## Env Vars

| Var | Purpose |
|---|---|
| `MESHAPI_API_KEY` | Mesh API data-plane key (`rsk_…`). Falls back to `MESH_API_KEY` for one release. |
| `MESHAPI_BASE_URL` | Override gateway URL. Default `https://api.meshapi.ai/v1`. |

Config at `~/.meshapi/config.json`. Input history at `~/.meshapi/history`.

## Architecture

Single-process REPL → stream `/v1/chat/completions` (SSE, OpenAI-compatible) → `rich.live.Live` markdown render → loop.

```
src/meshapi/
  cli.py        # argparse + REPL loop, prints cost line per turn
  client.py     # stream_chat — yields content deltas + final {usage, cost} dict
  commands.py   # slash command handlers (/model, /route, /file, /cost, ...)
  config.py     # ~/.meshapi/config.json load/save, env var override
  render.py     # rich Console singleton, render_stream, fmt_usd
  __main__.py   # python -m meshapi
```

## Mesh-specific conventions

- **Base URL:** `https://api.meshapi.ai/v1` (production).
- **Auth:** `Authorization: Bearer rsk_…` — `rsk_` is the data-plane key prefix.
- **Model format:** `provider/model-name` (e.g. `anthropic/claude-sonnet-4.5`, `openai/gpt-4o-mini`). See `meshapi-docs/fern/`.
- **Cost in stream:** the final SSE chunk includes a `cost` field (string USD) alongside `usage`. `client.stream_chat` captures it as the generator's last yield (a dict, not a string), which `render.render_stream` separates from content.
- **Routing:** request body accepts a `route` key (`cheapest`, `fastest`, `balanced`). Surfaced via `/route` slash command — Mesh's wedge over generic OpenAI-compat CLIs.

## Reusable utilities

- `render.fmt_usd(value)` — port of `fmtUsd` from `../routersvc-client/src/lib/utils.ts`. **Always 6 decimals** with K/M abbreviations. Use this for every USD amount; never raw `f"{n:.2f}"`. Keeps CLI cost display identical to the dashboard.

## Distribution

- **PyPI** (`meshapi-code`): `.github/workflows/publish.yml` builds and uploads on `v*` tag via [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (no token stored). Trusted Publisher must be set to `aifiesta/meshapi-code` repo + `publish.yml` workflow.
- **Install paths users will use:** `pipx install meshapi-code`, `uv tool install meshapi-code`, `pip install meshapi-code`.
- **npm port** (`meshapi-code`): planned. Node rewrite using `ink` + `chalk`. Same UX, ~200 LOC, no Python dep for JS users.
- **Out of scope for v0.1:** tool calling / file edits, diff apply, repo-aware mode, curl|sh installer, Homebrew tap, single-binary build.

## Testing the REPL end-to-end

```bash
MESHAPI_API_KEY=rsk_… meshapi
> hello                          # streamed markdown reply, then cost line
> /model openai/gpt-4o-mini      # switch model mid-session
> /route cheapest                # ask gateway to pick cheapest route
> /file ./pyproject.toml         # inject file into context
> /cost                          # show cumulative session spend
> /exit
```
