"""In-memory plan tracking — Claude Code-style checklist for multi-step work.

Plans live in `state["plan"]` and are replaced wholesale when the model calls
create_plan again. Only one step can be in_progress at a time (matches Claude
Code's behavior); promoting a different step to in_progress auto-completes
any other in-progress step.
"""
from dataclasses import dataclass

from .render import BRAND, BRAND_DIM, console

VALID_STATUSES = ("pending", "in_progress", "completed", "blocked")


@dataclass
class Step:
    title: str
    status: str = "pending"


class Plan:
    def __init__(self, titles):
        # Coerce any non-string entries to str so a malformed model response
        # can't corrupt the plan; drop empties to avoid blank checkboxes.
        self.steps = [Step(str(t).strip()) for t in titles if str(t).strip()]

    def update(self, index, status):
        """1-based index. Returns an error string, or None on success."""
        if not isinstance(index, int):
            return f"`index` must be an integer (got {type(index).__name__})"
        if not 1 <= index <= len(self.steps):
            return f"step {index} out of range (have {len(self.steps)} step(s))"
        if status not in VALID_STATUSES:
            return f"`status` must be one of {VALID_STATUSES}, got {status!r}"
        # Auto-finish any other in-progress step when starting a new one — only
        # one step is in_progress at a time, mirroring Claude Code's checklist.
        if status == "in_progress":
            for s in self.steps:
                if s.status == "in_progress":
                    s.status = "completed"
        self.steps[index - 1].status = status
        return None

    def render(self):
        """Print the checklist to the rich console."""
        console.print()
        console.print(f"  [bold {BRAND}]Plan[/bold {BRAND}]  [{BRAND_DIM}]{self.summary()}[/{BRAND_DIM}]")
        for i, step in enumerate(self.steps, 1):
            icon, style = _icon_style(step.status)
            console.print(f"    {icon}  [{style}]{i}. {step.title}[/{style}]")
        console.print()

    def summary(self):
        done = sum(1 for s in self.steps if s.status == "completed")
        return f"({done}/{len(self.steps)} done)"


def _icon_style(status):
    if status == "completed":
        return ("[green]✓[/green]", "dim")
    if status == "in_progress":
        return ("[bold yellow]▸[/bold yellow]", f"bold {BRAND}")
    if status == "blocked":
        return ("[red]✗[/red]", "red")
    return ("[dim]○[/dim]", "dim")
