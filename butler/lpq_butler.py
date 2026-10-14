#!/usr/bin/env python3
"""LPQ_VISION's butler (el mayordomo), SPEC 1.7: the host's one hand for the machine room.

A systemd service on the SERVER's host (butler/lpq-butler.service, installed in Section 3.18 by the deploy section's
listed commands; never a service on the laptop). Standard library only. Deaf to every network: it listens on ONE
unix-socket mailbox, BUTLER_SOCKET, whose directory is bind-mounted (read-only) into the fastapi container and nowhere
else.

The protocol: one plain line per connection ("reboot", "scale worker 2", "restart bot", each ending in a newline)
and one line back: "ok <id>" or "rechazado: <motivo>". A connection that sends nothing is the api's liveness probe:
it is answered by closing, and never logged.

The vocabulary is CLOSED: reboot, scale worker <1..4>, restart <fastapi|worker|bot>. Each line maps to ONE exact
command (no shell, no passthrough; see command_for). The answer goes out BEFORE the command runs: a restart of
fastapi or a reboot would otherwise kill the very answer the machine room waits for (and a reboot waits
REBOOT_PAUSE_S more, so the api writes its bitácora row before the host goes down). An answer that cannot be
delivered means the order is not run.

The lock (anti-repeat): reboot and each restart line are taken at most once per BUTLER_WINDOW_MIN minutes, per full
line ("restart bot" and "restart worker" are two locks). scale is exempt: it is idempotent, and the boot
reconvergence depends on it. The lock's memory is a root-only state file (STATE_FILE, systemd's StateDirectory),
written BEFORE the command runs, so it survives the very reboot it guards. A line that cannot be written there is
refused.

One order at a time: while a command runs, another line is refused; a probe is still answered.

--dry-run (the local test, SPEC 1.7): everything as above (the answers, the lock, one order at a time), but the
command is PRINTED instead of run. It runs inside a throwaway Linux container with the mailbox mounted, so the
laptop's real api proves the round trip before the server is ever touched.

A stop (SIGTERM: systemctl stop, docker stop) ends it cleanly and removes its socket, so the mailbox folder is left
empty.
"""
import argparse
import json
import logging
import math
import os
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

BUTLER_SOCKET = "/run/lpq-butler/butler.sock"   # the mailbox; its directory is the only window (fastapi's mount)
STATE_FILE = "/var/lib/lpq-butler/locks.json"   # the lock's memory: StateDirectory=lpq-butler, outside the mailbox
BUTLER_WINDOW_MIN = 10                          # one identical reboot or restart line per this many minutes
WORKER_SCALE_MAX = 4
PROJECT_DIR = "/opt/lpq_vision"                 # the server's deploy dir: the commands run here
COMPOSE_FILE = "compose.mac.yaml"               # always -f (CLAUDE.md rule 4)
DOCKER = "/usr/bin/docker"
SYSTEMCTL = "/usr/bin/systemctl"
COMMAND_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}
COMMAND_TIMEOUT_S = 180                         # a command still running after this is stopped
REBOOT_PAUSE_S = 5                              # from a reboot's ok to systemctl reboot: the api's row lands first
LINE_MAX = 64                                   # bytes of one line, its newline included
READ_S = 2.0                                    # a connected client sends its whole line within this time
OUTPUT_TAIL = 5                                 # lines of a failed command's output kept in the log

VERB_RE = re.compile(rf"^(reboot|scale worker [1-{WORKER_SCALE_MAX}]|restart (fastapi|worker|bot))\Z")

NO_SUCH_ORDER = "no conoce esa orden"
INCOMPLETE = "la orden llegó incompleta"
LOCKED = f"esa orden ya se dio hace menos de {BUTLER_WINDOW_MIN} min"
NOT_SAVED = f"no pudo guardar su candado de {BUTLER_WINDOW_MIN} min, así que no hizo nada"

log = logging.getLogger("lpq-butler")
_mailbox = {"inode": None}                      # the socket file this butler bound: the only one it ever removes


def command_for(line: str) -> list[str]:
    """The ONE exact command of a vocabulary line (the line was matched against VERB_RE)."""
    compose = [DOCKER, "compose", "-f", COMPOSE_FILE]
    if line == "reboot":
        return [SYSTEMCTL, "reboot"]
    verb, rest = line.split(" ", 1)
    if verb == "scale":
        count = int(rest.split(" ", 1)[1])
        if not 1 <= count <= WORKER_SCALE_MAX:
            raise ValueError(f"workers out of range: {count}")
        return compose + ["up", "-d", "--no-deps", "--no-recreate", "--no-build",
                          "--scale", f"worker={count}", "worker"]
    if verb == "restart" and rest in ("fastapi", "worker", "bot"):
        return compose + ["restart", "--no-deps", rest]
    raise ValueError(f"not a butler line: {line!r}")


def is_locked_line(line: str) -> bool:
    """reboot and restart hurt when repeated: they are locked. scale is not (idempotent)."""
    return line == "reboot" or line.startswith("restart ")


def new_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(2)


# --- the lock's memory -----------------------------------------------------------------------------------------

def load_locks() -> dict[str, float]:
    """line -> when it was last taken (seconds since the epoch). A missing file is an empty lock; an unreadable one
    too, said in the log (the next taken line writes it whole again)."""
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("not a table")
        locks = {}
        for line, taken_at in data.items():
            if isinstance(line, str) and isinstance(taken_at, (int, float)) and not isinstance(taken_at, bool):
                value = float(taken_at)
                if math.isfinite(value):
                    locks[line] = value
        return locks
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        log.warning("lock file %s unreadable (%s): counted as empty", STATE_FILE, type(exc).__name__)
        return {}


def save_locks(locks: dict[str, float]) -> None:
    """Write the whole table atomically (temporary file, fsync, rename, fsync of the directory), root-only."""
    directory = os.path.dirname(STATE_FILE)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(locks, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, STATE_FILE)
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def window_s() -> float:
    return BUTLER_WINDOW_MIN * 60.0


def lock_holds(taken_at: float | None, now: float) -> bool:
    """The lock is judged on the clock, within the window either side of when the line was taken: a clock that jumps
    back far frees the line instead of locking it for the whole jump (a small step back stretches the lock by that
    step)."""
    return taken_at is not None and abs(now - taken_at) < window_s()


# --- the butler ------------------------------------------------------------------------------------------------

class Butler:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self._busy = threading.Lock()   # held from the "ok" until the command ends: one order at a time
        self._current = ""              # the last line taken, for the busy refusal's words

    # Each connection: read one line, decide, answer, and only then act.
    def handle(self, conn: socket.socket) -> None:
        with conn:
            raw = self._read_line(conn)
            if raw is None:
                return                  # nothing sent: the liveness probe (or a client that left): never logged
            line, problem = self._parse(raw)
            if problem:
                log.warning("refused %r: %s", raw[:LINE_MAX].decode("ascii", "replace"), problem)
                self._answer(conn, "rechazado: " + problem)
                return
            if not self._busy.acquire(blocking=False):
                reason = f"todavía está con la orden anterior ({self._current}): intenta en un momento"
                log.info("refused %s: busy with %s", line, self._current)
                self._answer(conn, "rechazado: " + reason)
                return
            started = False
            try:
                started = self._take(conn, line)
            finally:
                if not started:
                    self._busy.release()

    def _read_line(self, conn: socket.socket) -> bytes | None:
        """Up to the first newline (kept), at most LINE_MAX bytes, all within READ_S. None when nothing came."""
        deadline = time.monotonic() + READ_S
        data = b""
        try:
            while b"\n" not in data and len(data) <= LINE_MAX:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                conn.settimeout(left)
                chunk = conn.recv(LINE_MAX + 1 - len(data))
                if not chunk:
                    break
                data += chunk
        except (TimeoutError, socket.timeout):
            pass
        except OSError:
            return None
        return data or None

    @staticmethod
    def _parse(raw: bytes) -> tuple[str, str]:
        """(line, "") for a vocabulary line, or ("", the refusal's reason)."""
        if b"\n" not in raw:
            return "", NO_SUCH_ORDER if len(raw) > LINE_MAX else INCOMPLETE
        first = raw.split(b"\n", 1)[0]
        if len(first) + 1 > LINE_MAX:
            return "", NO_SUCH_ORDER
        try:
            line = first.decode("ascii")
        except UnicodeDecodeError:
            return "", NO_SUCH_ORDER
        if not VERB_RE.match(line):
            return "", NO_SUCH_ORDER
        return line, ""

    def _take(self, conn: socket.socket, line: str) -> bool:
        """With the busy lock held: the anti-repeat lock (checked and written BEFORE anything runs), the answer, then
        the command in its own thread (a dry run prints it right here). True when that thread started: it gives the
        busy lock back itself."""
        argv = command_for(line)
        before: float | None = None
        locked = is_locked_line(line)
        now = time.time()
        if locked:
            locks = load_locks()
            before = locks.get(line)
            if lock_holds(before, now):
                log.info("refused %s: inside its %d-minute lock", line, BUTLER_WINDOW_MIN)
                self._answer(conn, "rechazado: " + LOCKED)
                return False
            locks = {k: v for k, v in locks.items() if lock_holds(v, now)}
            locks[line] = now
            try:
                save_locks(locks)
            except OSError as exc:
                log.error("refused %s: the lock file could not be written (%s)", line, type(exc).__name__)
                self._answer(conn, "rechazado: " + NOT_SAVED)
                return False
        ident = new_id()
        if not self._answer(conn, "ok " + ident):
            log.warning("%s %s: the answer did not reach the api, so it was not run", ident, line)
            if locked:
                self._undo_lock(line, before)
            return False
        log.info("%s %s: taken", ident, line)
        self._current = line
        if self.dry_run:                        # a print, done here: handle() gives the busy lock back
            print(f"DRY-RUN {ident} {line}: {' '.join(argv)} (in {PROJECT_DIR})", flush=True)
            return False
        threading.Thread(target=self._run, args=(ident, line, argv), name="lpq-butler-" + ident, daemon=True).start()
        return True

    @staticmethod
    def _undo_lock(line: str, before: float | None) -> None:
        try:
            locks = load_locks()
            if before is None:
                locks.pop(line, None)
            else:
                locks[line] = before
            save_locks(locks)
        except OSError as exc:
            log.warning("the lock of %s stays (%s)", line, type(exc).__name__)

    @staticmethod
    def _answer(conn: socket.socket, text: str) -> bool:
        try:
            conn.sendall((text + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    def _run(self, ident: str, line: str, argv: list[str]) -> None:
        try:
            if line == "reboot":
                log.info("%s reboot: the host reboots in %d s", ident, REBOOT_PAUSE_S)
                time.sleep(REBOOT_PAUSE_S)
            started = time.monotonic()
            try:
                done = subprocess.run(
                    argv, cwd=PROJECT_DIR, env=COMMAND_ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, timeout=COMMAND_TIMEOUT_S, check=False,
                )
            except subprocess.TimeoutExpired:
                log.error("%s %s: still running after %d s, stopped", ident, line, COMMAND_TIMEOUT_S)
                return
            except OSError as exc:
                log.error("%s %s: could not start (%s)", ident, line, type(exc).__name__)
                return
            took = time.monotonic() - started
            if done.returncode == 0:
                log.info("%s %s: done in %.1f s", ident, line, took)
                return
            tail = done.stdout.decode("utf-8", "replace").strip().splitlines()[-OUTPUT_TAIL:]
            log.error("%s %s: failed with exit %d in %.1f s: %s", ident, line, done.returncode, took, " | ".join(tail))
        except Exception:
            log.exception("%s %s: the run failed", ident, line)
        finally:
            self._busy.release()


# --- the mailbox -----------------------------------------------------------------------------------------------

def preflight(dry_run: bool) -> str:
    """Why the butler cannot serve ("" when it can): a missing command or project would answer "ok" to orders that
    then fail; better the room sees the butler red."""
    if dry_run:
        return ""
    for path in (DOCKER, SYSTEMCTL):
        if not os.access(path, os.X_OK):
            return f"{path} is missing or not executable"
    if not os.path.isfile(os.path.join(PROJECT_DIR, COMPOSE_FILE)):
        return f"{os.path.join(PROJECT_DIR, COMPOSE_FILE)} does not exist"
    return ""


def open_mailbox() -> socket.socket:
    """Bind BUTLER_SOCKET: its directory is created if missing (systemd creates it first, root-only); a stale socket
    left by an earlier run (a connect is refused there) is removed; a butler that answers there, anything that is not
    a socket, or a socket that cannot be judged stops this one."""
    directory = os.path.dirname(BUTLER_SOCKET)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        mode = os.lstat(BUTLER_SOCKET).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if not stat.S_ISSOCK(mode):
            raise SystemExit(f"{BUTLER_SOCKET} exists and is not a socket: not touched, not started")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            try:
                probe.connect(BUTLER_SOCKET)
            except ConnectionRefusedError:
                os.unlink(BUTLER_SOCKET)      # nobody listens there: a stale socket
            except FileNotFoundError:
                pass                          # it went away meanwhile
            except OSError as exc:
                raise SystemExit(f"cannot tell whether a butler answers at {BUTLER_SOCKET} ({type(exc).__name__}): "
                                 "not started") from None
            else:
                raise SystemExit(f"another butler already answers at {BUTLER_SOCKET}: not started")
    old_umask = os.umask(0o077)               # the socket is born root-only (the mount is the only window)
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(BUTLER_SOCKET)
    finally:
        os.umask(old_umask)
    _mailbox["inode"] = os.lstat(BUTLER_SOCKET).st_ino
    server.listen(8)
    return server


def close_mailbox(server: socket.socket) -> None:
    """Close the mailbox and remove its socket file, only while that file is still the one this butler bound."""
    server.close()
    try:
        if _mailbox["inode"] is not None and os.lstat(BUTLER_SOCKET).st_ino == _mailbox["inode"]:
            os.unlink(BUTLER_SOCKET)
    except OSError:
        pass
    _mailbox["inode"] = None


def serve(butler: Butler, server: socket.socket, stop: threading.Event | None = None) -> None:
    """Accept forever (or until stop is set): one connection at a time, each within READ_S."""
    server.settimeout(0.5 if stop is not None else None)
    while stop is None or not stop.is_set():
        try:
            conn, _ = server.accept()
        except (TimeoutError, socket.timeout):
            continue
        except OSError as exc:
            if stop is not None and stop.is_set():
                return
            log.error("accept failed (%s)", type(exc).__name__)
            time.sleep(0.5)
            continue
        try:
            butler.handle(conn)
        except Exception:
            log.exception("a connection failed")


def _stop(_signum, _frame) -> None:
    raise SystemExit(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LPQ_VISION's butler: the machine room's hand on the host.")
    parser.add_argument("--dry-run", action="store_true",
                        help="answer as usual but PRINT each command instead of running it (the local test)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    problem = preflight(args.dry_run)
    if problem:
        log.error("not started: %s", problem)
        return 1
    server = open_mailbox()
    signal.signal(signal.SIGTERM, _stop)      # systemctl stop or docker stop: a clean end, the socket removed
    if args.dry_run:
        log.info("listening on %s (dry run: each command is printed, never run)", BUTLER_SOCKET)
    else:
        log.info("listening on %s (commands run in %s)", BUTLER_SOCKET, PROJECT_DIR)
    try:
        serve(Butler(args.dry_run), server)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        close_mailbox(server)
        log.info("stopped: the mailbox is closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
