"""btop for Barry Launcher: the app's service (barry_apps).

QML has no terminal, so this runs btop in a pseudo-terminal, keeps the
screen it draws with a small xterm emulator (Screen), and serves that screen
to main.qml over HTTP on 127.0.0.1:BARRY_SERVICE_PORT. Standard library only.

  GET  /screen?gen=G&since=V   the rows changed since version V (all of them
                               if G is not the current generation; a resize
                               starts a new one). Waits up to a second for a
                               change, so the app can ask again at once.
  POST /resize {cols, rows}    set the terminal's size; the first one starts
                               btop, at that size
  POST /input  {data}          bytes to btop, as a keyboard would send them
  POST /mouse  {button, col, row, kind}
                               a click or wheel step at a cell (0-based),
                               sent the way btop asked for mouse events;
                               kind is "click" (the default), "press" or
                               "release"

Every request needs BARRY_SERVICE_TOKEN in X-Barry-Token.

btop is the one in the app folder (bin/btop, see package.sh), else the
system's. Its settings live in the app's data folder (XDG_CONFIG_HOME points
there), seeded from btop.conf in the app folder on first start.
"""
from __future__ import annotations

import codecs
import fcntl
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("BARRY_SERVICE_PORT", "0") or 0)
TOKEN = os.environ.get("BARRY_SERVICE_TOKEN", "")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BARRY_APP_DATA") or os.path.join(APP_DIR, ".data")

# Cell attributes (flags).
BOLD, DIM, ITALIC, UNDERLINE, REVERSE, HIDDEN, STRIKE = 1, 2, 4, 8, 16, 32, 64
DEFAULT = -1  # the default colour, fg or bg


def _palette() -> list[int]:
    """xterm's 256 colours as 0xRRGGBB."""
    base = [0x000000, 0xcd3131, 0x0dbc79, 0xe5e510, 0x2472c8, 0xbc3fbc, 0x11a8cd, 0xe5e5e5,
            0x666666, 0xf14c4c, 0x23d18b, 0xf5f543, 0x3b8eea, 0xd670d6, 0x29b8db, 0xffffff]
    steps = [0, 95, 135, 175, 215, 255]
    cube = [(steps[r] << 16) | (steps[g] << 8) | steps[b] for r in range(6) for g in range(6) for b in range(6)]
    grey = [(v << 16) | (v << 8) | v for v in (8 + 10 * i for i in range(24))]
    return base + cube + grey


PALETTE = _palette()


def char_width(ch: str) -> int:
    if ord(ch) < 0x300:
        return 1
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class Screen:
    """An xterm-like screen, enough for btop: cursor movement, erasing,
    scroll regions, insert/delete, SGR colours (16, 256 and truecolour), the
    alternate screen and mouse modes. Cells are [char, fg, bg, flags]; a
    wide character's second cell has char ""."""

    def __init__(self, cols: int, rows: int, reply):
        self.reply = reply  # writes answers (cursor reports) back to btop
        self.cols, self.rows = cols, rows
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = "ground"
        self.seq = ""
        self.mouse = 0      # 1000, 1002 or 1003 while btop wants mouse events
        self.mouse_sgr = False
        self.sync = False   # synchronized output (?2026) in progress
        self.reset()

    # -- state ------------------------------------------------------------

    def reset(self) -> None:
        self.fg, self.bg, self.flags = DEFAULT, DEFAULT, 0
        self.x = self.y = 0
        self.wrap_next = False
        self.autowrap = True
        self.origin = False
        self.top, self.bottom = 0, self.rows - 1
        self.saved = (0, 0, DEFAULT, DEFAULT, 0)
        self.grid = [self.new_row(DEFAULT) for _ in range(self.rows)]
        self.alt = None  # the main screen while the alternate one shows
        self.dirty = set(range(self.rows))

    def new_row(self, bg: int) -> list:
        return [[" ", DEFAULT, bg, 0] for _ in range(self.cols)]

    def resize(self, cols: int, rows: int) -> None:
        for grid in (self.grid, self.alt):
            if grid is None:
                continue
            for row in grid:
                if len(row) > cols:
                    del row[cols:]
                else:
                    row.extend([" ", DEFAULT, DEFAULT, 0] for _ in range(cols - len(row)))
            del grid[rows:]
            while len(grid) < rows:
                grid.append([[" ", DEFAULT, DEFAULT, 0] for _ in range(cols)])
        self.cols, self.rows = cols, rows
        self.x, self.y = min(self.x, cols - 1), min(self.y, rows - 1)
        self.top, self.bottom = 0, rows - 1
        self.wrap_next = False
        self.dirty = set(range(rows))

    # -- output from btop -------------------------------------------------

    def feed(self, data: bytes) -> None:
        text = self.decoder.decode(data)
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            st = self.state
            if st == "ground":
                if ch >= " " and ch != "\x7f":
                    # A run of printable characters at once.
                    j = i + 1
                    while j < n and text[j] >= " " and text[j] != "\x7f":
                        j += 1
                    for c in text[i:j]:
                        self.put(c)
                    i = j
                    continue
                self.control(ch)
            elif st == "esc":
                self.escape(ch)
            elif st == "csi":
                if "\x40" <= ch <= "\x7e":
                    self.state = "ground"
                    self.csi(self.seq, ch)
                elif ch == "\x1b":
                    self.state = "esc"
                elif ch < " ":
                    self.control(ch)
                else:
                    self.seq += ch
            elif st == "string":  # OSC, DCS, APC, PM, SOS: ignored up to BEL or ST
                if ch == "\x07":
                    self.state = "ground"
                elif ch == "\x1b":
                    self.state = "string-esc"
            elif st == "string-esc":
                self.state = "ground" if ch == "\\" else "string"
            elif st == "charset":  # ESC ( X and the like: X is ignored
                self.state = "ground"
            i += 1

    def control(self, ch: str) -> None:
        if ch == "\x1b":
            self.state = "esc"
        elif ch == "\r":
            self.x = 0
            self.wrap_next = False
        elif ch in "\n\x0b\x0c":
            self.linefeed()
        elif ch == "\b":
            if self.x > 0:
                self.x -= 1
            self.wrap_next = False
        elif ch == "\t":
            self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
            self.wrap_next = False

    def escape(self, ch: str) -> None:
        self.state = "ground"
        if ch == "[":
            self.state, self.seq = "csi", ""
        elif ch in "]PX^_":
            self.state = "string"
        elif ch in "()*+-./#%":
            self.state = "charset"
        elif ch == "7":
            self.save_cursor()
        elif ch == "8":
            self.restore_cursor()
        elif ch == "D":
            self.linefeed()
        elif ch == "E":
            self.x = 0
            self.linefeed()
        elif ch == "M":
            if self.y == self.top:
                self.scroll_down(1)
            elif self.y > 0:
                self.y -= 1
        elif ch == "c":
            self.reset()

    def put(self, ch: str) -> None:
        w = char_width(ch)
        if w == 0:  # combining: onto the previous cell
            x = self.x - 1 if self.x > 0 and not self.wrap_next else self.x
            cell = self.grid[self.y][x]
            if cell[0]:
                cell[0] += ch
            self.dirty.add(self.y)
            return
        if self.wrap_next:
            self.x = 0
            self.linefeed()
            self.wrap_next = False
        if w == 2 and self.x == self.cols - 1:
            if self.autowrap:
                self.grid[self.y][self.x] = [" ", DEFAULT, self.bg, 0]
                self.x = 0
                self.linefeed()
            else:
                return
        row = self.grid[self.y]
        row[self.x] = [ch, self.fg, self.bg, self.flags]
        if w == 2:
            row[self.x + 1] = ["", self.fg, self.bg, self.flags]
        self.dirty.add(self.y)
        if self.x + w >= self.cols:
            self.x = self.cols - 1
            self.wrap_next = self.autowrap
        else:
            self.x += w

    def linefeed(self) -> None:
        self.wrap_next = False
        if self.y == self.bottom:
            self.scroll_up(1)
        elif self.y < self.rows - 1:
            self.y += 1

    def scroll_up(self, n: int, top: int | None = None) -> None:
        top = self.top if top is None else top
        n = min(n, self.bottom - top + 1)
        for _ in range(n):
            del self.grid[top]
            self.grid.insert(self.bottom, self.new_row(self.bg))
        self.dirty.update(range(top, self.bottom + 1))

    def scroll_down(self, n: int, top: int | None = None) -> None:
        top = self.top if top is None else top
        n = min(n, self.bottom - top + 1)
        for _ in range(n):
            del self.grid[self.bottom]
            self.grid.insert(top, self.new_row(self.bg))
        self.dirty.update(range(top, self.bottom + 1))

    def erase(self, y: int, x0: int, x1: int) -> None:
        row = self.grid[y]
        for x in range(max(0, x0), min(self.cols, x1)):
            row[x] = [" ", DEFAULT, self.bg, 0]
        self.dirty.add(y)

    def save_cursor(self) -> None:
        self.saved = (self.x, self.y, self.fg, self.bg, self.flags)

    def restore_cursor(self) -> None:
        x, y, self.fg, self.bg, self.flags = self.saved
        self.x, self.y = min(x, self.cols - 1), min(y, self.rows - 1)
        self.wrap_next = False

    def move_to(self, y: int, x: int) -> None:
        if self.origin:
            y += self.top
        self.y = max(0, min(self.rows - 1, y))
        self.x = max(0, min(self.cols - 1, x))
        self.wrap_next = False

    def csi(self, seq: str, final: str) -> None:
        private = ""
        if seq and seq[0] in "?<=>":
            private, seq = seq[0], seq[1:]
        seq = seq.rstrip(" !\"#$%&'*+,-./")  # intermediates
        if final == "m" and not private:
            return self.sgr(seq)
        try:
            params = [int(p) if p else 0 for p in seq.replace(":", ";").split(";")] if seq else []
        except ValueError:
            return
        p0 = params[0] if params else 0
        n = max(1, p0)
        if private == "?":
            if final in "hl":
                for p in params:
                    self.private_mode(p, final == "h")
            return
        if private:
            return
        if final in "Hf":
            row = params[0] if params and params[0] else 1
            col = params[1] if len(params) > 1 and params[1] else 1
            self.move_to(row - 1, col - 1)
        elif final == "A":
            self.y = max(self.top if self.y >= self.top else 0, self.y - n)
            self.wrap_next = False
        elif final in "Be":
            self.y = min(self.bottom if self.y <= self.bottom else self.rows - 1, self.y + n)
            self.wrap_next = False
        elif final in "Ca":
            self.x = min(self.cols - 1, self.x + n)
            self.wrap_next = False
        elif final == "D":
            self.x = max(0, self.x - n)
            self.wrap_next = False
        elif final == "E":
            self.y = min(self.rows - 1, self.y + n)
            self.x = 0
            self.wrap_next = False
        elif final == "F":
            self.y = max(0, self.y - n)
            self.x = 0
            self.wrap_next = False
        elif final in "G`":
            self.x = max(0, min(self.cols - 1, n - 1))
            self.wrap_next = False
        elif final == "d":
            self.y = max(0, min(self.rows - 1, n - 1))
            self.wrap_next = False
        elif final == "J":
            if p0 == 0:
                self.erase(self.y, self.x, self.cols)
                for y in range(self.y + 1, self.rows):
                    self.erase(y, 0, self.cols)
            elif p0 == 1:
                self.erase(self.y, 0, self.x + 1)
                for y in range(0, self.y):
                    self.erase(y, 0, self.cols)
            elif p0 in (2, 3):
                for y in range(self.rows):
                    self.erase(y, 0, self.cols)
        elif final == "K":
            if p0 == 0:
                self.erase(self.y, self.x, self.cols)
            elif p0 == 1:
                self.erase(self.y, 0, self.x + 1)
            elif p0 == 2:
                self.erase(self.y, 0, self.cols)
        elif final == "X":
            self.erase(self.y, self.x, self.x + n)
        elif final == "@":
            row = self.grid[self.y]
            for _ in range(min(n, self.cols - self.x)):
                row.insert(self.x, [" ", DEFAULT, self.bg, 0])
                row.pop()
            self.dirty.add(self.y)
        elif final == "P":
            row = self.grid[self.y]
            for _ in range(min(n, self.cols - self.x)):
                row.pop(self.x)
                row.append([" ", DEFAULT, self.bg, 0])
            self.dirty.add(self.y)
        elif final == "L":
            if self.top <= self.y <= self.bottom:
                self.scroll_down(n, self.y)
                self.x = 0
        elif final == "M":
            if self.top <= self.y <= self.bottom:
                self.scroll_up(n, self.y)
                self.x = 0
        elif final == "S":
            self.scroll_up(n)
        elif final == "T" and len(params) <= 1:
            self.scroll_down(n)
        elif final == "b":
            prev = self.grid[self.y][self.x - 1][0] if self.x > 0 else " "
            for _ in range(min(n, self.cols)):
                self.put(prev or " ")
        elif final == "r":
            top = (params[0] - 1) if params and params[0] else 0
            bottom = (params[1] - 1) if len(params) > 1 and params[1] else self.rows - 1
            if 0 <= top < bottom < self.rows:
                self.top, self.bottom = top, bottom
                self.move_to(0, 0)
        elif final == "s":
            self.save_cursor()
        elif final == "u":
            self.restore_cursor()
        elif final == "n":
            if p0 == 6:
                self.reply(f"\x1b[{self.y + 1};{self.x + 1}R".encode())
            elif p0 == 5:
                self.reply(b"\x1b[0n")
        elif final == "c" and p0 == 0:
            self.reply(b"\x1b[?62;22c")

    def private_mode(self, mode: int, on: bool) -> None:
        if mode == 7:
            self.autowrap = on
        elif mode == 6:
            self.origin = on
            self.move_to(0, 0)
        elif mode in (1000, 1002, 1003):
            self.mouse = mode if on else 0
        elif mode == 1006:
            self.mouse_sgr = on
        elif mode == 2026:
            self.sync = on
        elif mode in (47, 1047, 1049):
            if on and self.alt is None:
                if mode == 1049:
                    self.save_cursor()
                self.alt = self.grid
                self.grid = [self.new_row(DEFAULT) for _ in range(self.rows)]
            elif not on and self.alt is not None:
                self.grid, self.alt = self.alt, None
                if mode == 1049:
                    self.restore_cursor()
            self.dirty = set(range(self.rows))

    def sgr(self, seq: str) -> None:
        parts = seq.replace(":", ";").split(";") if seq else ["0"]
        try:
            ps = [int(p) if p else 0 for p in parts]
        except ValueError:
            return
        i = 0
        while i < len(ps):
            p = ps[i]
            if p == 0:
                self.fg, self.bg, self.flags = DEFAULT, DEFAULT, 0
            elif p == 1:
                self.flags |= BOLD
            elif p == 2:
                self.flags |= DIM
            elif p == 3:
                self.flags |= ITALIC
            elif p in (4, 21):
                self.flags |= UNDERLINE
            elif p == 7:
                self.flags |= REVERSE
            elif p == 8:
                self.flags |= HIDDEN
            elif p == 9:
                self.flags |= STRIKE
            elif p == 22:
                self.flags &= ~(BOLD | DIM)
            elif p == 23:
                self.flags &= ~ITALIC
            elif p == 24:
                self.flags &= ~UNDERLINE
            elif p == 27:
                self.flags &= ~REVERSE
            elif p == 28:
                self.flags &= ~HIDDEN
            elif p == 29:
                self.flags &= ~STRIKE
            elif 30 <= p <= 37:
                self.fg = PALETTE[p - 30]
            elif 40 <= p <= 47:
                self.bg = PALETTE[p - 40]
            elif 90 <= p <= 97:
                self.fg = PALETTE[p - 90 + 8]
            elif 100 <= p <= 107:
                self.bg = PALETTE[p - 100 + 8]
            elif p == 39:
                self.fg = DEFAULT
            elif p == 49:
                self.bg = DEFAULT
            elif p in (38, 48, 58):
                colour = None
                if i + 2 < len(ps) and ps[i + 1] == 5:
                    colour = PALETTE[ps[i + 2] & 255]
                    i += 2
                elif i + 1 < len(ps) and ps[i + 1] == 2 and i + 4 < len(ps):
                    r, g, b = ps[i + 2] & 255, ps[i + 3] & 255, ps[i + 4] & 255
                    colour = (r << 16) | (g << 8) | b
                    i += 4
                if colour is not None:
                    if p == 38:
                        self.fg = colour
                    elif p == 48:
                        self.bg = colour
            i += 1

    # -- for the app ------------------------------------------------------

    def row_runs(self, y: int) -> list:
        """Row y as runs of cells that look alike: [col, text, cells, fg,
        bg, flags], colours as "#rrggbb" or "" for the default. Blank cells
        on the default background are left out."""
        runs = []
        row = self.grid[y]
        x = 0
        while x < self.cols:
            ch, fg, bg, fl = row[x]
            start = x
            text = [ch]
            x += 1
            while x < self.cols:
                c = row[x]
                if c[1] != fg or c[2] != bg or c[3] != fl:
                    break
                text.append(c[0])
                x += 1
            s = "".join(text)
            if fl & REVERSE:
                fg, bg = (bg if bg != DEFAULT else -2), (fg if fg != DEFAULT else -3)
            if bg == DEFAULT and (not s.strip() or fl & HIDDEN):
                continue
            runs.append([start, s, x - start, _hex(fg), _hex(bg), fl & ~REVERSE])
        return runs


def _hex(c: int) -> str:
    if c == -2:
        return "bg"  # reverse video of the default colours
    if c == -3:
        return "fg"
    return "" if c == DEFAULT else f"#{c:06x}"


class Btop:
    """btop in a pseudo-terminal, its screen, and versions of that screen
    for the app to ask for changes."""

    def __init__(self):
        self.lock = threading.Condition()
        self.screen: Screen | None = None
        self.proc: subprocess.Popen | None = None
        self.fd = -1
        self.version = 0
        self.gen = 0
        self.row_ver: list[int] = []
        self.exit_code: int | None = None
        self.error = ""

    def write(self, data: bytes) -> None:
        fd = self.fd
        if fd < 0:
            return
        try:
            os.write(fd, data)
        except OSError:
            pass

    def find(self) -> str | None:
        bundled = os.path.join(APP_DIR, "bin", "btop")
        if os.path.isfile(bundled):
            # Archives install without the exec bit: run a copy that has it.
            dest = os.path.join(DATA, "bin", "btop")
            try:
                if not os.path.isfile(dest) or os.path.getmtime(dest) < os.path.getmtime(bundled) \
                        or os.path.getsize(dest) != os.path.getsize(bundled):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copyfile(bundled, dest + ".new")
                    os.chmod(dest + ".new", 0o755)
                    os.replace(dest + ".new", dest)
                return dest
            except OSError as err:
                print(f"btop: cannot use the bundled btop: {err}", flush=True)
        found = shutil.which("btop")
        if found:
            return found
        for p in (os.path.expanduser("~/.local/bin/btop"), "/usr/local/bin/btop", "/usr/bin/btop"):
            if os.access(p, os.X_OK):
                return p
        return None

    def seed_config(self, env: dict) -> None:
        conf_dir = os.path.join(env.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "btop")
        conf = os.path.join(conf_dir, "btop.conf")
        seed = os.path.join(APP_DIR, "btop.conf")
        if not os.path.exists(conf) and os.path.isfile(seed):
            os.makedirs(conf_dir, exist_ok=True)
            shutil.copyfile(seed, conf)

    def start(self, cols: int, rows: int) -> None:
        exe = self.find()
        if not exe:
            self.error = "btop is not installed. Install it with your package manager, " \
                         "or use a build of this app that brings its own (package.sh --with-btop)."
            self.bump(all_rows=False)
            return
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        env.pop("BARRY_SERVICE_TOKEN", None)
        loc = env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or ""
        if "utf-8" not in loc.lower() and "utf8" not in loc.lower():
            env["LC_ALL"] = "C.UTF-8"  # btop will not start without UTF-8
        self.seed_config(env)
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        def controlling_tty():
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self.proc = subprocess.Popen([exe], stdin=slave, stdout=slave, stderr=slave, env=env,
                                         cwd=DATA if os.path.isdir(DATA) else None,
                                         start_new_session=True, preexec_fn=controlling_tty, close_fds=True)
        except OSError as err:
            os.close(master)
            os.close(slave)
            self.error = f"btop did not start: {err}"
            self.bump(all_rows=False)
            return
        os.close(slave)
        self.fd = master
        print(f"btop: started {exe} (pid {self.proc.pid}) at {cols}x{rows}", flush=True)
        threading.Thread(target=self.read_loop, daemon=True).start()

    def read_loop(self) -> None:
        fd = self.fd
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self.lock:
                self.screen.feed(data)
                # In a synchronized update, show it when it is done (or the
                # next read that ends one).
                if not self.screen.sync:
                    self.bump()
        code = self.proc.wait() if self.proc else None
        with self.lock:
            self.exit_code = code if code is not None else 0
            print(f"btop: ended ({self.exit_code})", flush=True)
            self.bump(all_rows=False)

    def bump(self, all_rows: bool = False) -> None:
        """A new version: the screen's changed rows carry it. Holds lock."""
        self.version += 1
        sc = self.screen
        if sc is not None:
            rows = range(sc.rows) if all_rows else sc.dirty
            for y in rows:
                if y < len(self.row_ver):
                    self.row_ver[y] = self.version
            sc.dirty = set()
        self.lock.notify_all()

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(20, min(400, cols)), max(8, min(200, rows))
        with self.lock:
            if self.screen is None:
                self.screen = Screen(cols, rows, self.write)
                self.row_ver = [0] * rows
                self.gen += 1
                self.start(cols, rows)
                return
            if (cols, rows) == (self.screen.cols, self.screen.rows):
                return
            self.screen.resize(cols, rows)
            self.row_ver = [0] * rows
            self.gen += 1
            self.bump(all_rows=True)
        if self.fd >= 0:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def mouse(self, button: int, col: int, row: int, kind: str) -> None:
        """kind: "click" (a press and its release, in one write so nothing
        comes between them), "press" or "release". Wheels (64, 65) only
        press."""
        sc = self.screen
        if sc is None or not sc.mouse:
            return
        presses = {"click": (True, False), "press": (True,), "release": (False,)}.get(kind, ())
        if button >= 64:
            presses = presses[:1] if presses and presses[0] else ()
        out = b""
        for press in presses:
            if sc.mouse_sgr:
                out += f"\x1b[<{button};{col + 1};{row + 1}{'M' if press else 'm'}".encode()
            else:
                b = button if press else 3
                out += b"\x1b[M" + bytes([32 + b, min(255, 33 + col), min(255, 33 + row)])
        self.write(out)

    def snapshot(self, gen: int, since: int, wait: float) -> dict:
        deadline = time.monotonic() + wait
        with self.lock:
            while True:
                fresh = gen != self.gen or self.version > since
                left = deadline - time.monotonic()
                if fresh or left <= 0:
                    break
                self.lock.wait(left)
            sc = self.screen
            out = {"v": self.version, "gen": self.gen, "running": self.exit_code is None and self.fd >= 0,
                   "exit": self.exit_code, "error": self.error, "mouse": bool(sc and sc.mouse)}
            if sc is None:
                out.update(cols=0, rows=0, full=True, lines=[])
                return out
            full = gen != self.gen
            out.update(cols=sc.cols, rows=sc.rows, full=full,
                       lines=[[y, sc.row_runs(y)] for y in range(sc.rows)
                              if full or self.row_ver[y] > since])
            return out

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass


BTOP = Btop()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet: the app asks many times a second
        pass

    def allowed(self, query: dict) -> bool:
        token = self.headers.get("X-Barry-Token") or (query.get("token") or [""])[0]
        if TOKEN and token == TOKEN:
            return True
        self.send(403, {"error": "forbidden"})
        return False

    def send(self, status: int, body) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the app closed while it waited for a change

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            v = json.loads(self.rfile.read(min(n, 1 << 20)))
        except ValueError:
            return {}
        return v if isinstance(v, dict) else {}

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if not self.allowed(q):
            return
        if url.path == "/screen":
            def num(k, d):
                try:
                    return int(q.get(k, [d])[0])
                except ValueError:
                    return d
            self.send(200, BTOP.snapshot(num("gen", -1), num("since", -1), min(2.0, num("wait", 1000) / 1000)))
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if not self.allowed(parse_qs(url.query)):
            return
        b = self.body()
        try:
            if url.path == "/resize":
                BTOP.resize(int(b["cols"]), int(b["rows"]))
            elif url.path == "/input":
                BTOP.write(str(b.get("data", "")).encode())
            elif url.path == "/mouse":
                BTOP.mouse(int(b.get("button", 0)), int(b["col"]), int(b["row"]), str(b.get("kind", "click")))
            else:
                return self.send(404, {"error": "not found"})
        except (KeyError, TypeError, ValueError) as err:
            return self.send(400, {"error": str(err)})
        self.send(200, {"ok": True})


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True

    def stop(*_):
        BTOP.stop()
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"btop service on 127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    finally:
        BTOP.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
