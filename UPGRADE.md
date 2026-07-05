# Upgrading `meshapi` to 0.5.1

**0.5.1 is a major update** — first-run key setup, an update checker, real
auto-routing, a fuzzy model picker, web search, an agentic quality guard, and
a Claude Code-style live input. Full list below.

> Package name on PyPI is **`meshapi-code`**; the command it installs is **`meshapi`**.

---

## Quick upgrade

Use whichever tool you originally installed with. The commands are the same on every OS
(run them in **PowerShell** on Windows, **Terminal** on macOS/Linux).

| Installed with | Upgrade command |
|---|---|
| **pipx** (recommended) | `pipx upgrade meshapi-code` |
| **uv** | `uv tool upgrade meshapi-code` |
| **pip** | `pip install --upgrade meshapi-code` |

Not sure how you installed it? Try `pipx list` — if `meshapi-code` shows up, use pipx.

Then **verify**:

```
meshapi --version      # -> meshapi 0.5.1
```

If it still shows an old version, jump to [Troubleshooting](#troubleshooting).
From 0.5.1 onward the CLI checks for updates itself and offers to upgrade.

---

## What's new in 0.5.1

**Getting started**
- First run walks you through connecting your API key (hidden input, live
  verification, saved to `~/.meshapi/credentials`). `/login` replaces it.
- The CLI checks PyPI in the background and offers one-key upgrades
  (`/update` checks on demand; declining a version never re-nags).

**Models & routing**
- `/model` has **fuzzy tab-completion**: type `/model qw` and pick from every
  qwen model. `/models [free|query]` browses the catalog with context sizes
  and $/1M pricing.
- `/route auto` — the gateway's router picks the best model per prompt
  (`route: cheapest|fastest|balanced` never worked server-side and is gone).
  `/fallback m1 m2` sets an ordered fallback list; `/reasoning high|…` sets
  reasoning effort.
- The agent can now **search the web** (gated by permission modes).

**Agentic reliability**
- Malformed tool calls are repaired client-side instead of burning retries;
  the model never re-reads its own broken output (ends the retry doom-loop,
  biggest win on cheaper models).
- A **quality guard** catches stub code ("// Add game logic here"): one
  automatic fix-it pass, then an honest warning naming the files + a model
  suggestion — no more "Server's up!" over a blank page.
- `start_server` detects the port inside your command, adopts whatever port
  the server actually bound, shows progress while waiting, and never
  orphans processes on ctrl+c.

**Terminal experience**
- **Type while it works**: the input line stays live during streaming;
  Enter stacks messages that auto-run in order; unfinished text prefills
  the next prompt. **ESC aborts** a running turn.
- Permission mode is always visible (and shift+tab applies mid-run); answer
  `a` at any approval to allow that tool for the session.
- Framed input with repo · git-branch title, streaming header with live
  token counts, background servers listed under the mode line.

---

## Troubleshooting

### pipx says: `The uv backend was requested but the 'uv' executable could not be found`

Recent pipx versions default to the `uv` backend. If `uv` isn't on your PATH, force pip:

```
pipx upgrade meshapi-code --backend pip
```

If pipx ignores `--backend pip` because the venv was created with `uv`, recreate it:

```
pipx uninstall meshapi-code
pipx install meshapi-code --backend pip
```

(Or set `PIPX_DEFAULT_BACKEND=pip` in your shell profile.)

### `meshapi --version` still shows an old version

A second, older copy earlier on your PATH is shadowing the new one. Find every copy:

- **macOS / Linux:** `which -a meshapi`
- **Windows (PowerShell):** `where.exe meshapi`

The **first** one wins. Remove a stray old copy (often a `pip install --user` leftover):

```
pip uninstall meshapi-code
```

Then refresh the shell cache (`hash -r` on macOS/Linux, or open a new terminal).

### Still stuck?

```
pipx uninstall meshapi-code
pipx install meshapi-code       # add --backend pip if you hit the uv error above
```

Full release notes: <https://github.com/aifiesta/meshapi-code/releases>
