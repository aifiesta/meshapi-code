# meshapi-code

Terminal chat REPL for [Mesh API](https://meshapi.ai) — one OpenAI-compatible key, 300+ models. Streaming responses, live markdown, file/shell tool calls with approval, real-time cost.

```
$ meshapi
███╗   ███╗███████╗███████╗██╗  ██╗   ✦  meshapi 0.3.0
████╗ ████║██╔════╝██╔════╝██║  ██║   cwd:   ~/code/myproj
██╔████╔██║█████╗  ███████╗███████║   model: anthropic/claude-sonnet-4.5
██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║   route: cheapest
██║ ╚═╝ ██║███████╗███████║██║  ██║
╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝
type /help for commands, /exit to quit

› add a healthcheck endpoint to server.py and run the tests
… streamed markdown reply …
⚙ approve tool call?  write_file: server.py (1240 chars)   y/n › y
⚙ approve tool call?  run_bash: pytest -q                  y/n › y
   anthropic/claude-sonnet-4.5  •  942→318 tok  •  $0.001234  •  session $0.001234
   mode: approve each   model can request file/shell ops; you confirm each one   shift+tab to cycle
```

## Install

```bash
pipx install meshapi-code           # recommended
uv tool install meshapi-code        # if you use uv
pip install meshapi-code            # plain pip
```

PyPI package is `meshapi-code`; the command on your `$PATH` is `meshapi` (same split Claude Code uses: `@anthropic-ai/claude-code` → `claude`).

```bash
export MESHAPI_API_KEY=rsk_your_key_here
meshapi
```

Get a key at [meshapi.ai](https://meshapi.ai).

## What it does

- **Streaming completions** with live markdown rendering (`rich`).
- **Real cost per turn** — Mesh returns `cost` in the SSE tail; we surface it after every reply and accumulate `session $…`.
- **Tool calling** — the model can read files, write files, and run shell commands in the launch directory. Off by default behind an approval prompt; toggle with one key.
- **Permission modes** — `approve each` (default), `bypass perms` (auto-execute, for trusted prompts), or `no access` (chat only). Cycle live with **Shift+Tab**.
- **Mid-session switching** — `/model openai/gpt-4o-mini`, `/route cheapest`, `/mode bypass`.
- **Smart routing** — `/route cheapest|fastest|balanced` hands model selection to Mesh's gateway, so you don't have to.
- **Persistent input history** — up-arrow recalls past prompts across sessions.
- **Config + env-var override** — `~/.meshapi/config.json`, `MESHAPI_API_KEY`.

## Tool calling

When tools are enabled, the model can call:

| Tool | What it does |
|---|---|
| `read_file` | Read a file from the working directory (or absolute path). |
| `write_file` | Create or overwrite a file. Parent dirs are created. |
| `run_bash` | Run a shell command in the working directory. 60s timeout, 8000-char output cap. |

The launch CWD is baked into the system prompt, so relative paths the model produces resolve where you'd expect. Three permission modes, cycled live with Shift+Tab or set with `--mode` / `/mode`:

- **`ask`** (default) — every tool call requires a `y/n` confirmation. Safe.
- **`bypass`** — the model auto-executes. Fast, like Claude Code's `--dangerously-skip-permissions`. Use only when you trust the prompt.
- **`none`** — tools aren't sent to the model at all. Pure chat.

```bash
meshapi --mode bypass     # start in auto-execute mode
meshapi                   # default ask; press Shift+Tab to cycle
```

## Slash commands

| Command | What it does |
|---|---|
| `/help` | List commands |
| `/model <name>` | Switch model (e.g. `anthropic/claude-sonnet-4.5`, `openai/gpt-4o-mini`) |
| `/route <mode>` | `cheapest`, `fastest`, `balanced`, or `default` |
| `/mode <perm>` | `ask`, `bypass`, or `none` (Shift+Tab also cycles) |
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

The API key is read from `MESHAPI_API_KEY` (preferred) or stored in the same file. Input history lives at `~/.meshapi/history`.

## About Mesh API

[Mesh API](https://meshapi.ai) is a unified LLM gateway: one API key, 300+ models from OpenAI, Anthropic, Google, Meta, Mistral, DeepSeek, Alibaba, and more. It's OpenAI-compatible — change the model name in your request, leave everything else alone.

- **Zero platform fees for 12 months.** You only pay for tokens.
- **Smart auto routing.** `route: cheapest|fastest|balanced` and the gateway picks for you.
- **Automatic failover.** If a provider goes down, your request routes to another. Your users won't know.
- **Highest rate limits.** Capacity is pooled across providers, so you hit ceilings later than going direct.
- **Zero data retention.** Prompts and completions pass through; we don't store them.
- **Multi-currency billing.** USD and INR (for India-based teams) at launch.
- **Ready-made workflows.** Pre-built prompt templates you can plug into any model.
- **Full observability.** Every request, token, cost, error, and model usage tracked in real time. Per-key spending limits and usage controls.

Built by the founders of [TagMango](https://tagmango.com) (YC W20) and [AI Fiesta](https://aifiesta.ai) (1M+ users across India). We got tired of managing five different provider dashboards ourselves, so we built this.

## Why this CLI exists

Any generic OpenAI-compatible chat CLI talks to Mesh. `meshapi` adds three things a generic CLI can't: (1) the gateway-only `cost` field shown after every turn, (2) `/route` controls that drive Mesh's gateway-side model selection, and (3) tool calling that resolves paths against the directory you launched from.

## Roadmap

- ✅ v0.3 — tool calling, ask/bypass/none permission modes, CWD-aware system prompt
- v0.4 — repo-aware mode, diff apply, `/cd` to change working dir mid-session
- v0.5 — `npm i -g meshapi-code` (Node port using `ink` + `chalk`), Homebrew tap, curl|sh installer at `meshapi.ai/install.sh`

## License

[Apache 2.0](LICENSE)
