"""Background stdin watcher: shift+tab, type-ahead capture, and queueing
outside of prompt_toolkit.

The prompt-side bindings only fire while `session.prompt(...)` is the active
reader of stdin. Between turns — while the model streams, while tools run —
a daemon thread reads stdin in cbreak mode and parses it with `_InputParser`:
shift+tab cycles the mode, printable text accumulates in the type-ahead
buffer (rendered live by the streaming footer), Enter submits the buffer to
the input queue via `on_submit`, and bare ESC fires `on_esc`.

The watcher pauses automatically around `session.prompt(...)` via the
`paused()` context manager: termios is restored to its original line-edit
state so prompt_toolkit can configure stdin however it likes, then we
re-enter cbreak when control returns.

Thread-safety (no locks — single-writer discipline):
- `_typeahead` is written ONLY by the watcher thread; other threads read the
  `.typeahead` property (an atomic str reference under the GIL). The one
  exception, `take_typeahead()`, must be called only while `paused()` — the
  watcher thread isn't reading stdin then, so it's structurally race-free.
- Callbacks run on the watcher thread and must only mutate state via
  GIL-atomic operations (deque.append, Event.set, enum assignment).

Caveats:
- If stdin isn't a TTY (piped input, headless), the watcher is a no-op —
  no type-ahead, no queue, no esc.
- cbreak re-entry uses TCSAFLUSH: bytes typed during the ms-scale
  pause→resume transition are discarded (pre-existing behavior).
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


# Shift+Tab → CSI Z (back-tab), 7-bit ESC [ Z only. The 8-bit C1 form
# (0x9b) is deliberately NOT recognized anymore: 0x9b is a valid UTF-8
# continuation byte (e.g. `›` = e2 80 ba), so treating it as CSI would
# corrupt pasted UTF-8 text. Modern terminals never send 8-bit C1 as input.


class _InputParser:
    """Pure byte-level parser for raw cbreak stdin. Zero I/O — feed() bytes,
    get back a list of events; unit-testable without a terminal.

    Events: ("text", str) · ("backspace",) · ("kill_line",) · ("submit",) ·
            ("shift_tab",) · ("esc",)

    States: GROUND / ESC / CSI / SS3 / OSC. Escape sequences other than
    shift+tab (arrows, function keys, bracketed-paste markers) are filtered
    and dropped so they never pollute the type-ahead buffer.

    Enter-vs-paste heuristic: a newline that ends a chunk becomes
    `pending_newline` — resolved to ("submit",) by on_timeout() (the select
    loop's ~30ms peek found nothing after it = a real keypress), or to a
    literal "\\n" if more bytes arrive first (= mid-paste). This is what
    keeps a multi-line paste from becoming N queued messages = N API calls.
    """

    GROUND, ESC, CSI, SS3, OSC = range(5)

    def __init__(self) -> None:
        import codecs
        self._decoder_factory = codecs.getincrementaldecoder("utf-8")
        self.reset()

    def reset(self) -> None:
        """Drop in-flight escape/pending state (called on pause). The
        type-ahead buffer itself lives in the watcher and is preserved."""
        self._state = self.GROUND
        self._decoder = self._decoder_factory(errors="replace")
        self._csi_len = 0
        self._csi_poisoned = False  # runaway sequence: swallow to final byte
        self._osc_len = 0
        self._osc_esc = False
        self._pending_newline = False
        self._swallow_lf = False  # \n immediately after a consumed \r

    @property
    def has_pending(self) -> bool:
        """True when a timeout is needed to resolve state — the select loop
        shortens its timeout to ~30ms while this is set."""
        return self._pending_newline or self._state != self.GROUND

    def on_timeout(self) -> list:
        """No bytes arrived within the peek window — resolve pendings."""
        events = []
        if self._pending_newline:
            self._pending_newline = False
            events.append(("submit",))
        if self._state == self.ESC:
            self._state = self.GROUND
            events.append(("esc",))
        elif self._state in (self.CSI, self.SS3, self.OSC):
            self._state = self.GROUND  # abandon a stalled sequence
        return events

    def _flush_text(self, out: list, raw: bytearray) -> None:
        if raw:
            s = self._decoder.decode(bytes(raw))
            if s:
                out.append(("text", s))
            raw.clear()

    def _normalize(self, chunk: bytes) -> bytes:
        """CR and CRLF → LF, across chunk boundaries, BEFORE the state
        machine — so 'last byte of the chunk' semantics see one newline per
        Enter regardless of how the terminal encodes it or where the read
        split it."""
        out = bytearray()
        for b in chunk:
            if self._swallow_lf:
                self._swallow_lf = False
                if b == 0x0A:
                    continue
            if b == 0x0D:
                self._swallow_lf = True
                out.append(0x0A)
            else:
                out.append(b)
        return bytes(out)

    def feed(self, chunk: bytes) -> list:
        chunk = self._normalize(chunk)
        if not chunk:
            return []
        events: list = []
        raw = bytearray()  # printable bytes awaiting UTF-8 decode

        if self._pending_newline:
            # More bytes arrived right behind the newline — it was a paste,
            # not an Enter keypress: keep it as literal text.
            self._pending_newline = False
            events.append(("text", "\n"))

        n = len(chunk)
        i = 0
        while i < n:
            b = chunk[i]
            last = i == n - 1
            if self._state == self.GROUND:
                if b == 0x1B:
                    self._flush_text(events, raw)
                    self._state = self.ESC
                elif b == 0x0A:
                    self._flush_text(events, raw)
                    if last:
                        self._pending_newline = True
                    else:
                        events.append(("text", "\n"))
                elif b in (0x7F, 0x08):
                    self._flush_text(events, raw)
                    events.append(("backspace",))
                elif b == 0x15:  # Ctrl+U
                    self._flush_text(events, raw)
                    events.append(("kill_line",))
                elif b < 0x20:
                    self._flush_text(events, raw)  # other C0 → drop
                else:
                    raw.append(b)  # printable ASCII + all >=0x80 (incl. 0x9b)
            elif self._state == self.ESC:
                if b == 0x5B:  # [
                    self._state = self.CSI
                    self._csi_len = 0
                    self._csi_poisoned = False
                elif b == 0x4F:  # O
                    self._state = self.SS3
                elif b == 0x5D:  # ]
                    self._state = self.OSC
                    self._osc_len = 0
                    self._osc_esc = False
                elif b == 0x1B:
                    events.append(("esc",))  # first ESC resolved; stay in ESC
                else:
                    self._state = self.GROUND  # Alt+key — drop both
            elif self._state == self.CSI:
                self._csi_len += 1
                if 0x40 <= b <= 0x7E:  # final byte
                    if b == 0x5A and self._csi_len == 1 and not self._csi_poisoned:
                        events.append(("shift_tab",))  # bare CSI Z
                    self._state = self.GROUND  # everything else dropped
                elif self._csi_len > 32:
                    # Runaway sequence — poison it and swallow through the
                    # final byte so param junk never leaks into type-ahead.
                    self._csi_poisoned = True
            elif self._state == self.SS3:
                self._state = self.GROUND  # consume exactly one byte
            elif self._state == self.OSC:
                self._osc_len += 1
                if b == 0x07:  # BEL terminator
                    self._state = self.GROUND
                elif self._osc_esc and b == 0x5C:  # ESC \ (ST)
                    self._state = self.GROUND
                else:
                    self._osc_esc = b == 0x1B
                    if self._osc_len > 256:
                        self._state = self.GROUND
            i += 1
        self._flush_text(events, raw)
        return events


_TYPEAHEAD_CAP = 65536  # chars — bound a runaway paste


class KeyWatcher:
    """Daemon thread reading stdin for out-of-prompt input capture."""

    def __init__(self, on_shift_tab, on_submit=None, on_esc=None) -> None:
        self._on_shift_tab = on_shift_tab
        self._on_submit = on_submit
        self._on_esc = on_esc
        self._parser = _InputParser()
        self._typeahead = ""
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

    # ---- type-ahead access ------------------------------------------------

    @property
    def typeahead(self) -> str:
        """Current un-submitted type-ahead. Atomic str reference — safe to
        read from any thread (may be one frame stale, never corrupt)."""
        return self._typeahead

    def take_typeahead(self) -> str:
        """Return-and-clear the buffer. CONTRACT: call only while paused()
        — the watcher thread isn't reading stdin then (race-free)."""
        text = self._typeahead
        self._typeahead = ""
        return text

    def _apply_events(self, events: list) -> None:
        for ev in events:
            kind = ev[0]
            try:
                if kind == "text":
                    if len(self._typeahead) < _TYPEAHEAD_CAP:
                        self._typeahead += ev[1]
                elif kind == "backspace":
                    self._typeahead = self._typeahead[:-1]
                elif kind == "kill_line":
                    self._typeahead = ""
                elif kind == "submit":
                    text = self._typeahead.strip()
                    self._typeahead = ""
                    if text and self._on_submit is not None:
                        self._on_submit(text)
                elif kind == "shift_tab":
                    self._on_shift_tab()
                elif kind == "esc":
                    if self._on_esc is not None:
                        self._on_esc()
            except Exception:  # pragma: no cover — callbacks must never kill the loop
                pass

    # ---- pause / resume around prompt_toolkit ---------------------------

    @contextlib.contextmanager
    def paused(self):
        """Context manager: pause the watcher (restore canonical mode) while
        the prompt is active, then re-enter cbreak when the prompt returns.

        We wake the select loop so it drops stdin from its fd list before
        prompt_toolkit starts reading — otherwise we'd race for the same
        kernel buffer and swallow bytes (like the CPR response)."""
        self._active.clear()
        self._parser.reset()  # drop in-flight escape state; typeahead survives
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
            # Short timeout while the parser is mid-sequence (pending Enter
            # or an unfinished ESC) so the resolve happens within ~30ms —
            # that's the Enter-vs-paste and bare-ESC timer.
            timeout = 0.03 if self._parser.has_pending else 0.5
            try:
                ready, _, _ = select.select(fds, [], [], timeout)
            except (ValueError, OSError):
                break
            if not ready:
                if self._active.is_set():
                    self._apply_events(self._parser.on_timeout())
                continue
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
                # 1024, not 32: paste bursts must land in as few chunks as
                # possible for the Enter-vs-paste heuristic to see them whole.
                chunk = os.read(self._fd, 1024)
            except (OSError, ValueError, BlockingIOError):
                continue
            if not chunk:
                continue
            self._apply_events(self._parser.feed(chunk))
