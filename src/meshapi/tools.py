"""Tool definitions sent to the model + local executors."""
import subprocess
from pathlib import Path


def build_system_prompt(cfg: dict) -> str:
    """Append working-dir + tool guidance to the user's base system prompt.

    Naming tools in prose (read_file/write_file/run_bash) makes Anthropic
    models drop into XML tool-use mode and emit `<function_calls>` as
    text — keep this section deliberately tool-name-free.
    """
    base = cfg.get("system") or ""
    cwd = str(Path.cwd())
    return (
        f"{base}\n\n"
        f"Working directory: {cwd}\n"
        "Resolve any relative path the user gives against this working "
        "directory. When you create or edit files without an explicit "
        "absolute path, place them inside this working directory. Use "
        "the available tools to inspect and modify the filesystem and "
        "run shell commands — do not ask the user to run commands."
    )

# OpenAI-compatible tool spec — Mesh API forwards these to the underlying provider.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the user's filesystem and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file (absolute, or relative to the cwd)",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content. Parent directories are created if missing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Full file contents"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a shell command (zsh/bash) and return combined stdout+stderr plus exit code. Times out at 60s.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run"}
                },
                "required": ["command"],
            },
        },
    },
]

OUTPUT_LIMIT = 8000


def execute(name: str, arguments: dict) -> str:
    """Run a tool locally and return a string result for the model."""
    if name == "read_file":
        try:
            return Path(arguments["path"]).expanduser().read_text()
        except Exception as e:
            return f"Error: {e}"

    if name == "write_file":
        try:
            p = Path(arguments["path"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            content = arguments["content"]
            p.write_text(content)
            return f"OK — wrote {len(content)} chars to {p}"
        except Exception as e:
            return f"Error: {e}"

    if name == "run_bash":
        try:
            r = subprocess.run(
                arguments["command"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(Path.cwd()),
            )
            out = (r.stdout or "") + (r.stderr or "")
            tail = "...[truncated]" if len(out) > OUTPUT_LIMIT else ""
            return f"{out[:OUTPUT_LIMIT]}{tail}\n[exit {r.returncode}]"
        except subprocess.TimeoutExpired:
            return "Error: command timed out after 60s"
        except Exception as e:
            return f"Error: {e}"

    return f"Error: unknown tool `{name}`"


def summarize_call(name: str, arguments: dict) -> str:
    """One-line summary used in the approval prompt and progress log."""
    if name == "read_file":
        return f"read_file: {arguments.get('path')}"
    if name == "write_file":
        n = len(arguments.get("content", ""))
        return f"write_file: {arguments.get('path')} ({n} chars)"
    if name == "run_bash":
        cmd = arguments.get("command", "")
        if len(cmd) > 200:
            cmd = cmd[:200] + "…"
        return f"run_bash: {cmd}"
    return f"{name}({arguments})"
