# meshapi-code

Terminal chat for [Mesh API](https://meshapi.ai) — the OpenAI-compatible LLM gateway. Streaming responses, live markdown, slash commands, real-time cost.

```
$ meshapi
╭───────────────────────────────╮
│ meshapi 0.1.0                 │
│ model: anthropic/claude-…     │
│ route: default                │
╰───────────────────────────────╯
you > how do I parse SSE in python
… streamed markdown reply …
   142 → 318 tok  •  $0.001234  •  session $0.001234
```

## Install

```bash
pipx install meshapi-code           # recommended
uv tool install meshapi-code        # if you use uv
pip install meshapi-code            # plain pip
```

The PyPI package is `meshapi-code`; the command on your `$PATH` is `meshapi`.

Then:

```bash
export MESHAPI_API_KEY=rsk_your_key_here
meshapi
```

Get a key at [meshapi.ai](https://meshapi.ai).

## What it does

- **Streaming completions** with live markdown rendering (`rich`)
- **Real cost per turn** — Mesh API returns `cost` in the SSE tail; we show it
- **Slash commands** — `/model`, `/route`, `/file`, `/system`, `/cost`, `/clear`
- **Mid-session model switching** — `/model openai/gpt-4o-mini`
- **Smart routing** — `/route cheapest` lets the gateway pick (Mesh-specific)
- **Persistent input history** — up-arrow recalls past prompts
- **Config + env-var override** — `~/.meshapi/config.json`, `MESHAPI_API_KEY`

## Slash commands

| Command | What it does |
|---|---|
| `/help` | List commands |
| `/model <name>` | Switch model (e.g. `anthropic/claude-sonnet-4.5`) |
| `/route <mode>` | `cheapest`, `fastest`, `balanced`, or `default` |
| `/file <path>` | Inject a file into the conversation |
| `/system <text>` | Replace system prompt and reset chat |
| `/cost` | Show cumulative session spend |
| `/clear` | Reset conversation |
| `/exit` | Quit |

## Config

`~/.meshapi/config.json`:

```json
{
  "base_url": "https://api.meshapi.ai/v1",
  "model": "anthropic/claude-sonnet-4.5",
  "system": "You are a helpful coding assistant. Be concise.",
  "route": null
}
```

The API key is read from `MESHAPI_API_KEY` (preferred) or stored in the same file.

## Why it exists

Mesh API is OpenAI-compatible, so any generic chat CLI works against it. `meshapi` adds two things a generic CLI can't: (1) the gateway-only `cost` field shown after every turn, and (2) routing controls (`/route cheapest`) that hit Mesh's gateway-side model selection.

## Roadmap

- v0.2 — tool calling, repo-aware mode, diff apply, `npm i -g meshapi-code`
- v0.3 — Homebrew tap, curl|sh installer at `meshapi.ai/install.sh`

## License

MIT
