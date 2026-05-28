"""Background stdin watcher for shift+tab outside of prompt_toolkit.

The prompt-side shift+tab binding only fires while `session.prompt(...)` is
the active reader of stdin. Between turns — while the model is streaming,
while tool calls are executing, while npm is installing — stdin is otherwise
idle, so we run a daemon thread that reads it in cbreak mode and fires a
callback when it sees the shift+tab escape sequence (CSI Z).

The watcher pauses automatically around `session.prompt(...)` via the
`paused()` context manager: termios is restored to its original line-edit
state so prompt_toolkit can configure stdin however it likes, then we
re-enter cbreak when control returns.

Caveats:
- Type-ahead during model execution is consumed by the watcher and not
  forwarded to the next prompt. That's a deliberate trade-off — most users
  don't type while waiting, and forwarding raw bytes to prompt_toolkit is
  fragile.
- If stdin isn't a TTY (piped input, headless), the watcher is a no-op.
"""
import atexit
import contextlib
import os
import select
import sys
import threading

try:  # POSIX-only; the CLI is documented Mac/Linux but be defensive.
    import termios
    import tty
    _HAVE_TERMIOS = True
except ImportError:  # pragma: no cover — Windows / unusual platforms
    _HAVE_TERMIOS = False


# Shift+Tab → CSI Z (back-tab). Covers macOS Terminal, iTerm2, Alacritty,
# kitty, tmux, ghostty. We tolerate either the 7-bit ESC-prefixed form or
# the 8-bit C1 form (rare).
_SHIFT_TAB_VARIANTS = (b"\x1b[Z", b"\x9bZ")


class KeyWatcher:
    """Daemon thread reading stdin for out-of-prompt key bindings."""

    def __init__(self, on_shift_tab) -> None:
        self._on_shift_tab = on_shift_tab
        self._fd = None
        self._saved_termios = None
        self._thread = None
        self._stop = threading.Event()
        self._active = threading.Event()
        self._lock = threading.Lock()
        # Self-pipe: when paused/resumed we write a byte here so the select
        # loop returns immediately and re-evaluates whether to include stdin
        # in its fd list. Without this, the watcher could be mid-select on
        # stdin when pause() is called and swallow bytes meant for
        # prompt_toolkit (e.g. its CPR response, causing the
        # "your terminal doesn't support cursor position requests" warning).
        self._wake_r = -1
        self._wake_w = -1

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Begin watching stdin. No-op if stdin isn't a TTY or termios is missing."""
        if not _HAVE_TERMIOS:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._active.set()
                self._enter_cbreak()
                self._wake()
                return
            try:
                self._fd = sys.stdin.fileno()
                if not os.isatty(self._fd):
                    return
                self._saved_termios = termios.tcgetattr(self._fd)
            except (termios.error, ValueError, AttributeError, OSError):
                return  # not a TTY or stdin is closed
            try:
                self._wake_r, self._wake_w = os.pipe()
            except OSError:
                return
            self._enter_cbreak()
            self._stop.clear()
            self._active.set()
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="meshapi-keywatcher"
            )
            self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        """Stop the watcher and restore the original termios state."""
        self._stop.set()
        self._active.clear()
        self._wake()
        self._restore_termios()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        # Close the self-pipe.
        for fd_attr in ("_wake_w", "_wake_r"):
            fd = getattr(self, fd_attr)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_attr, -1)

    # ---- pause / resume around prompt_toolkit ---------------------------

    @contextlib.contextmanager
    def paused(self):
        """Context manager: pause the watcher (restore canonical mode) while
        the prompt is active, then re-enter cbreak when the prompt returns.

        We wake the select loop so it drops stdin from its fd list before
        prompt_toolkit starts reading — otherwise we'd race for the same
        kernel buffer and swallow bytes (like the CPR response)."""
        self._active.clear()
        self._restore_termios()
        self._wake()
        try:
            yield
        finally:
            self._enter_cbreak()
            self._active.set()
            self._wake()

    # ---- internals -------------------------------------------------------

    def _enter_cbreak(self) -> None:
        if self._fd is None or self._saved_termios is None:
            return
        try:
            tty.setcbreak(self._fd)
        except (termios.error, ValueError, OSError):
            pass

    def _wake(self) -> None:
        """Push a byte through the self-pipe so the select loop returns
        and re-evaluates whether to include stdin in its fd list."""
        if self._wake_w < 0:
            return
        try:
            os.write(self._wake_w, b"x")
        except (OSError, BlockingIOError):
            pass

    def _restore_termios(self) -> None:
        if self._fd is None or self._saved_termios is None:
            return
        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_termios)
        except (termios.error, ValueError, OSError):
            pass

    def _loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            # Build the fd list per-iteration: always listen on the wake pipe,
            # add stdin only when active. While paused, stdin is left for
            # prompt_toolkit to consume — kernel buffers any pending bytes
            # (including the CPR response) until prompt_toolkit reads them.
            fds = []
            if self._wake_r >= 0:
                fds.append(self._wake_r)
            if self._active.is_set() and self._fd is not None:
                fds.append(self._fd)
            if not fds:
                self._stop.wait(0.1)
                continue
            try:
                ready, _, _ = select.select(fds, [], [], 0.5)
            except (ValueError, OSError):
                break
            # Drain the wake pipe and re-evaluate state at the top of the loop.
            if self._wake_r >= 0 and self._wake_r in ready:
                try:
                    os.read(self._wake_r, 64)
                except OSError:
                    pass
                continue
            if self._fd is None or self._fd not in ready:
                continue
            try:
                chunk = os.read(self._fd, 32)
            except (OSError, ValueError, BlockingIOError):
                continue
            if not chunk:
                continue
            buf += chunk
            # Drain every shift+tab in the buffer (user could chord rapidly).
            fired = True
            while fired:
                fired = False
                for marker in _SHIFT_TAB_VARIANTS:
                    idx = buf.find(marker)
                    if idx >= 0:
                        buf = buf[idx + len(marker):]
                        try:
                            self._on_shift_tab()
                        except Exception:  # pragma: no cover — defensive
                            pass
                        fired = True
                        break
            # Bound the buffer so random keystrokes (which we silently drop)
            # don't grow it forever.
            if len(buf) > 128:
                buf = buf[-32:]
