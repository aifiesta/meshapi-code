# Installing `meshapi` — complete guide (Windows & macOS)

This walks you from **nothing installed** to a working `meshapi` command.

> **Package name:** `meshapi-code` (on PyPI) → **command:** `meshapi`
> (same split as Claude Code: package `@anthropic-ai/claude-code`, command `claude`.)

## Quick install (recommended)

One command installs everything — **no Python, no pipx, no per-OS setup.** It installs
[uv](https://astral.sh) (a single binary that brings its own Python), installs `meshapi`,
puts it on your `PATH`, and launches it. Re-run it any time to upgrade.

**macOS / Linux (Terminal):**

```bash
curl -fsSL https://cli.meshapi.ai/install.sh | sh
```

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://cli.meshapi.ai/install.ps1 | iex"
```

On first launch you'll be asked for your Mesh API key (starts with `rsk_`, from
<https://app.meshapi.ai>) — hidden input, verified live, saved to `~/.meshapi/credentials`.

That's it. **The rest of this guide is the manual install** — use it only for offline or
locked-down machines, or if you'd rather not run a `curl … | sh` one-liner. The scripts are
short and inspectable:
[`install.sh`](https://github.com/aifiesta/meshapi-code/blob/main/install.sh) ·
[`install.ps1`](https://github.com/aifiesta/meshapi-code/blob/main/install.ps1).

---

# Manual install

The per-OS walkthrough below installs Python → pipx → meshapi by hand.

## What you need

| Requirement | Notes |
|---|---|
| **Python 3.10 or newer** | 3.10 – 3.13 supported. We install this in Step 1. |
| **pipx** | Recommended installer — isolates the CLI in its own environment. Step 2. |
| **A Mesh API key** | Starts with `rsk_`. Get one at <https://app.meshapi.ai>. You don't set this up by hand — **meshapi asks for it on the first run** (Step 4). |

The CLI's Python dependencies (`httpx`, `rich`, `prompt-toolkit`) install
automatically — you don't install those by hand.

Jump to your OS: [🪟 Windows](#-windows) · [🍎 macOS](#-macos).

---

# 🪟 Windows

Use **PowerShell** for every command below (Start menu → type "PowerShell" → Enter).

## Step 1 — Install Python

1. Download the latest Python 3.12 installer from <https://www.python.org/downloads/windows/>.
2. Run it. On the **first screen, check ✅ "Add python.exe to PATH"** (this matters — skip it
   and the commands below won't be found), then click **Install Now**.
3. Close and reopen PowerShell, then confirm:

   ```powershell
   python --version
   ```

   You should see `Python 3.12.x` (any 3.10+ is fine).

## Step 2 — Install pipx

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

**Close and reopen PowerShell** (so the updated PATH takes effect), then verify:

```powershell
pipx --version
```

## Step 3 — Install meshapi

```powershell
pipx install meshapi-code
```

Verify:

```powershell
meshapi --version        # -> meshapi 0.5.6
```

If PowerShell says `meshapi` is not recognized, run `pipx ensurepath` again, then close
and reopen the window.

## Step 4 — Run it (first launch sets up your key)

```powershell
meshapi
```

There's **nothing to configure first.** On the very first run, meshapi asks for your Mesh
API key, verifies it live, and saves it — then drops you at the prompt. Here's exactly what
you'll see:

```
PS C:\Users\you> meshapi
╭─────────────────────────────────────────────────────────────────────╮
│ Connect your Mesh API key                                           │
│                                                                     │
│ Grab one at https://app.meshapi.ai → API Keys. Keys start with rsk_ │
│ Input is hidden — paste the key and press enter. Ctrl+C to cancel.  │
╰─────────────────────────────────────────────────────────────────────╯
API key › ••••••••••••  (hidden as you paste)
✓ key saved → C:\Users\you\.meshapi\credentials

███╗   ███╗███████╗███████╗██╗  ██╗
████╗ ████║██╔════╝██╔════╝██║  ██║
██╔████╔██║█████╗  ███████╗███████║   ✦  meshapi 0.5.6
██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║   cwd:   C:\Users\you\projects\hello
██║ ╚═╝ ██║███████╗███████║██║  ██║   model: anthropic/claude-sonnet-4.5
╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝   route: off

type /help for commands, /exit to quit
›
```

Type `/help` for commands, `/exit` to quit. To change the key later, run `/login` inside
the REPL. **No environment variable needed.**

<details>
<summary><b>Prefer an environment variable?</b> (CI, scripts, shared machines)</summary>

A non-interactive shell (a CI job, a piped command) can't show the key prompt, so pass the
key with the `MESHAPI_API_KEY` env var instead:

```powershell
setx MESHAPI_API_KEY "rsk_your_key_here"      # persistent — reopen PowerShell afterward
$env:MESHAPI_API_KEY = "rsk_your_key_here"    # current window only
```

When set, the env var takes precedence over the saved credentials file.
</details>

---

# 🍎 macOS

Use the **Terminal** app (Applications → Utilities → Terminal). Commands assume **zsh**
(the macOS default).

## Step 1 — Install Python

Easiest via [Homebrew](https://brew.sh). If you don't have Homebrew:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Then install Python:

```bash
brew install python
python3 --version          # -> Python 3.12.x (any 3.10+ is fine)
```

> No Homebrew? You can instead download the macOS installer from
> <https://www.python.org/downloads/macos/>.

## Step 2 — Install pipx

```bash
brew install pipx
pipx ensurepath
```

**Open a new Terminal window** (so PATH updates), then verify:

```bash
pipx --version
```

## Step 3 — Install meshapi

```bash
pipx install meshapi-code
```

Verify:

```bash
meshapi --version          # -> meshapi 0.5.6
```

## Step 4 — Run it (first launch sets up your key)

```bash
meshapi
```

There's **nothing to configure first.** On the very first run, meshapi asks for your Mesh
API key, verifies it live, and saves it — then drops you at the prompt. Here's exactly what
you'll see:

```
$ meshapi
╭─────────────────────────────────────────────────────────────────────╮
│ Connect your Mesh API key                                           │
│                                                                     │
│ Grab one at https://app.meshapi.ai → API Keys. Keys start with rsk_ │
│ Input is hidden — paste the key and press enter. Ctrl+C to cancel.  │
╰─────────────────────────────────────────────────────────────────────╯
API key › ••••••••••••  (hidden as you paste)
✓ key saved → ~/.meshapi/credentials (0600)

███╗   ███╗███████╗███████╗██╗  ██╗
████╗ ████║██╔════╝██╔════╝██║  ██║
██╔████╔██║█████╗  ███████╗███████║   ✦  meshapi 0.5.6
██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║   cwd:   ~/projects/hello
██║ ╚═╝ ██║███████╗███████║██║  ██║   model: anthropic/claude-sonnet-4.5
╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝   route: off

type /help for commands, /exit to quit
›
```

Type `/help` for commands, `/exit` to quit. To change the key later, run `/login` inside
the REPL. **No environment variable needed.**

<details>
<summary><b>Prefer an environment variable?</b> (CI, scripts, shared machines)</summary>

A non-interactive shell (a CI job, a piped command) can't show the key prompt, so pass the
key with the `MESHAPI_API_KEY` env var instead:

```bash
echo 'export MESHAPI_API_KEY="rsk_your_key_here"' >> ~/.zshrc && source ~/.zshrc  # persistent
export MESHAPI_API_KEY="rsk_your_key_here"                                        # this session only
```

When set, the env var takes precedence over the saved credentials file.
</details>

---

## First-run check (both platforms)

The first `meshapi` launch handles the key for you (shown above). After that, a quick smoke
test:

```
meshapi --version     # meshapi 0.5.6
meshapi               # launches straight into the REPL
> hello               # streams a reply, then prints a cost line
> /model openai/gpt-4o-mini
> /exit
```

Need to change the key later? Run `/login` in the REPL. Only if you're on a
**non-interactive** shell (CI) where meshapi can't prompt will you see `No API key found` —
set `MESHAPI_API_KEY` (see the env-var note under Step 4) and reopen the terminal.

---

## Alternative installers

`pipx` is recommended because it isolates the CLI. If you prefer another tool:

| Tool | Install | Upgrade |
|---|---|---|
| **uv** | `uv tool install meshapi-code` | `uv tool upgrade meshapi-code` |
| **pip** | `pip install meshapi-code` | `pip install --upgrade meshapi-code` |

Plain `pip` installs into whatever Python environment is active and can collide with other
packages — prefer `pipx` or `uv` unless you're installing inside a dedicated virtualenv.

---

## Optional settings

| Env var | Purpose |
|---|---|
| `MESHAPI_API_KEY` | Your `rsk_…` data-plane key. **Optional** — the first run saves it to `~/.meshapi/credentials` for you; set this only for CI/scripts. Overrides the saved key when present. |
| `MESHAPI_BASE_URL` | Override the gateway URL. Default `https://api.meshapi.ai/v1`. |

State lives under `~/.meshapi/` (`credentials` for the key, `config.json` for settings —
never the key — plus input history), all written with `0600` permissions.

---

## Troubleshooting

### `meshapi: command not found` / not recognized
The install directory isn't on your PATH yet. Run `pipx ensurepath`, then **close and
reopen** the terminal. On Windows, make sure you checked "Add python.exe to PATH" in Step 1.

### pipx error: `The uv backend was requested but the 'uv' executable could not be found`
Recent pipx versions default to the `uv` backend. Force pip instead:

```
pipx install meshapi-code --backend pip
```

If pipx says it's *ignoring* `--backend pip` for an existing venv, recreate it:

```
pipx uninstall meshapi-code
pipx install meshapi-code --backend pip
```

### `meshapi --version` shows an older version than you just installed
A second, older copy earlier on your PATH is shadowing the new one. List all of them:

- **macOS/Linux:** `which -a meshapi`
- **Windows:** `where.exe meshapi`

The **first** path wins. Remove the stray older copy (often an old `pip install --user`)
with `pip uninstall meshapi-code` run by the Python that owns it, then reopen your terminal
(or `hash -r` on macOS/Linux).

### `No API key found …`
On a normal terminal you never hit this — meshapi *asks* for the key on first run. You'll
only see it on a **non-interactive** shell (a CI job, a piped command, or an IDE terminal
that isn't a real TTY) where it can't prompt. Fix it either way:

- Run `meshapi` once in a normal terminal to save the key to `~/.meshapi/credentials`, or
- Set the `MESHAPI_API_KEY` env var (see the note under Step 4) and open a **new** terminal.

---

## Upgrading & uninstalling

- **Upgrade:** re-run the install one-liner, or `meshapi upgrade` (auto-detects uv/pipx/pip).
  Manual: `uv tool upgrade meshapi-code` / `pipx upgrade meshapi-code` / `pip install -U meshapi-code`.
  See [UPGRADE.md](UPGRADE.md) for details.
- **Uninstall:** `uv tool uninstall meshapi-code` (installer/uv) or `pipx uninstall meshapi-code`.

---

- Homepage: <https://meshapi.ai> · Docs: <https://docs.meshapi.ai>
- Source & releases: <https://github.com/aifiesta/meshapi-code>
