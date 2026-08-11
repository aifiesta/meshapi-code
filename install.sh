#!/bin/sh
# meshapi installer  —  macOS & Linux
#
#   curl -fsSL https://cli.meshapi.ai/install.sh | sh
#
# Installs uv (a single static binary that brings its own Python), then
# `uv tool install meshapi-code`, fixes PATH, and launches meshapi. Nothing
# needs to be preinstalled — no Python, no pipx. Re-running upgrades an
# existing install.
#
# Source: https://github.com/aifiesta/meshapi-code/blob/main/install.sh
# Inspect before piping to a shell — this file is short on purpose.
set -eu

PKG="meshapi-code"
UV_VERSION="0.12.2"   # pinned for supply-chain safety; bump deliberately

info() { printf '\033[36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# --- make uv (and its tool shims) reachable in THIS shell -----------------
# uv installs to ~/.local/bin on current releases; older uv used ~/.cargo/bin.
# It also drops an env script that puts its bin dir on PATH — source it if it
# exists (guard -u: env files reference vars we haven't set).
ensure_uv_on_path() {
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    case ":$PATH:" in
      *":$d:"*) ;;
      *) [ -d "$d" ] && PATH="$d:$PATH" ;;
    esac
  done
  if [ -f "$HOME/.local/bin/env" ]; then
    set +u; . "$HOME/.local/bin/env"; set -u
  fi
  export PATH
}

command -v curl >/dev/null 2>&1 || die "curl is required (install it, then re-run)."

ensure_uv_on_path
if ! command -v uv >/dev/null 2>&1; then
  info "Installing uv (Python bootstrapper)…"
  # Pinned, HTTPS, its own curl|sh — independent of this script's stdin.
  curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh \
    || die "uv install failed — see https://docs.astral.sh/uv/getting-started/installation/"
  ensure_uv_on_path
fi
command -v uv >/dev/null 2>&1 \
  || die "uv is installed but not on PATH — open a new terminal and re-run this command."

# --- warn if a non-uv meshapi is about to be shadowed ---------------------
if command -v meshapi >/dev/null 2>&1; then
  cur=$(command -v meshapi)
  case "$cur" in
    *"uv/tools"*|*"/.local/bin/meshapi") : ;;   # ours, or the shim uv will own
    *) warn "Note: found an existing meshapi at $cur (not uv-managed). uv will take over the 'meshapi' command; if an old copy still shadows it later, remove it (e.g. pipx uninstall meshapi-code)." ;;
  esac
fi

# --- install, or upgrade if already present (idempotent) ------------------
if uv tool list 2>/dev/null | grep -q "^${PKG} "; then
  info "Upgrading ${PKG}…"
  uv tool upgrade "${PKG}" || die "upgrade failed"
else
  info "Installing ${PKG}…"
  uv tool install "${PKG}" || die "install failed"
fi

# Persist PATH for future shells, then refresh this one so the launch below
# and a plain `meshapi` both work without reopening the terminal.
uv tool update-shell >/dev/null 2>&1 || true
ensure_uv_on_path
command -v meshapi >/dev/null 2>&1 || die "meshapi not found after install."

info "✓ $(meshapi --version 2>/dev/null || echo "${PKG} installed")"

# --- launch on the controlling terminal, else print how to start ----------
# In `curl … | sh`, this script's stdin is the pipe (not a TTY), so meshapi's
# first-run key prompt would exit immediately. Reconnect stdin to the real
# terminal, gated on an interactive session ([ -t 1 ] => a human is watching).
# `exec` as the last statement means sh never reads more of the piped script.
if [ -t 1 ] && [ -r /dev/tty ]; then
  info "Launching meshapi… (first run asks for your rsk_ API key)"
  exec meshapi </dev/tty
else
  info "Installed. Start it any time with:  meshapi"
  info "(If 'meshapi' isn't found, open a new terminal first.)"
fi
