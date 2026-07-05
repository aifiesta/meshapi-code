# Installing `meshapi` — complete guide (Windows & macOS)

This walks you from **nothing installed** to a working `meshapi` command, on both
Windows and macOS. It covers installing Python, installing the installer (`pipx`),
installing the CLI, and adding your Mesh API key.

> **Package name:** `meshapi-code` (on PyPI) → **command:** `meshapi`
> (same split as Claude Code: package `@anthropic-ai/claude-code`, command `claude`.)

## What you need

| Requirement | Notes |
|---|---|
| **Python 3.10 or newer** | 3.10 – 3.13 supported. We install this in Step 1. |
| **A Mesh API key** | Starts with `rsk_`. Get one at <https://app.meshapi.ai>. Step 4. |
| **pipx** | Recommended installer — isolates the CLI in its own environment. Step 2. |

The CLI's Python dependencies (`httpx`, `rich`, `prompt-toolkit`) install
automatically — you don't install those by hand.

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
meshapi --version        # -> meshapi 0.5.1
```

If PowerShell says `meshapi` is not recognized, run `pipx ensurepath` again, then close
and reopen the window.

## Step 4 — Add your Mesh API key

Get a key (starts with `rsk_`) from <https://app.meshapi.ai>, then set it **persistently**:

```powershell
setx MESHAPI_API_KEY "rsk_your_key_here"
```

`setx` only affects **new** terminals — **close and reopen PowerShell** afterward.
(To use it in the *current* window without reopening: `$env:MESHAPI_API_KEY = "rsk_your_key_here"`.)

## Step 5 — Run it

```powershell
meshapi
```

You should see the MESH banner and a `›` prompt. Type `/help` for commands, `/exit` to quit.

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
meshapi --version          # -> meshapi 0.5.1
```

## Step 4 — Add your Mesh API key

Get a key (starts with `rsk_`) from <https://app.meshapi.ai>, then add it to your shell
profile so it's set in every terminal:

```bash
echo 'export MESHAPI_API_KEY="rsk_your_key_here"' >> ~/.zshrc
source ~/.zshrc
```

(Just this session, no profile edit: `export MESHAPI_API_KEY="rsk_your_key_here"`.)

## Step 5 — Run it

```bash
meshapi
```

You should see the MESH banner and a `›` prompt. Type `/help` for commands, `/exit` to quit.

---

## First-run check (both platforms)

```
meshapi --version     # meshapi 0.5.1
meshapi               # launches the REPL
> hello               # streams a reply, then prints a cost line
> /model openai/gpt-4o-mini
> /exit
```

If you see **`No API key found`**, your `MESHAPI_API_KEY` isn't set in *this* terminal —
revisit Step 4 and reopen the terminal (env vars only apply to terminals opened after
they're set).

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
| `MESHAPI_API_KEY` | Your `rsk_…` data-plane key (**required**). |
| `MESHAPI_BASE_URL` | Override the gateway URL. Default `https://api.meshapi.ai/v1`. |

State lives under `~/.meshapi/` (`config.json` for settings — never your key — plus input
history), all written with `0600` permissions.

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

### `No API key found. Set MESHAPI_API_KEY …`
The key isn't set in the current terminal. Redo Step 4 and open a **new** terminal — env
vars only apply to sessions started after they were set.

---

## Upgrading & uninstalling

- **Upgrade:** `pipx upgrade meshapi-code` (or `uv tool upgrade` / `pip install -U`).
  See [UPGRADE.md](UPGRADE.md) for details and the 0.5.1 notes.
- **Uninstall:** `pipx uninstall meshapi-code`.

---

- Homepage: <https://meshapi.ai> · Docs: <https://docs.meshapi.ai>
- Source & releases: <https://github.com/aifiesta/meshapi-code>
