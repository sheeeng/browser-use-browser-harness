"""Cross-platform daemon IPC: AF_UNIX socket on POSIX, TCP loopback on Windows.

Stdlib-only. POSIX uses a Unix-domain stream socket at <tempdir>/bu-<NAME>.sock
(filesystem-namespaced, ACL'd via chmod 0600). Windows uses an ephemeral TCP
port on 127.0.0.1, with the chosen port written to <tempdir>/bu-<NAME>.port
so clients can find it. Both expose a newline-delimited JSON request/response
protocol; one short-lived connection per request.

Note on uv-Python and AF_UNIX: python-build-standalone (the distribution uv
ships) on Windows is compiled without `socket.AF_UNIX`. We never reference
that attribute on Windows -- every call is gated on IS_WINDOWS -- so this
module imports cleanly on uv-Python-Windows.

Public surface (used by daemon.py, admin.py, helpers.py):

    sock_addr(name)   -- POSIX socket path, or "127.0.0.1:<port>" once the
                         port file exists on Windows. Display-only.
    log_path(name)    -- Path under tempdir for daemon stdout log
    pid_path(name)    -- Path under tempdir for daemon pid file
    port_path(name)   -- Path under tempdir for the .port sidecar (Windows only;
                         exists on POSIX too as a no-op so callers don't branch)

    connect(name, timeout=1.0) -> socket
        Returns a connected blocking socket. Raises FileNotFoundError if no
        daemon (no socket file / no port file), TimeoutError on connect timeout.

    serve(name, handler) -> coroutine
        Runs the server until cancelled. `handler(reader, writer)` is the
        standard asyncio stream callback; transport is invisible to it.

    cleanup_endpoint(name) -- best-effort removal of POSIX socket file or
                              Windows .port file.

    spawn_kwargs() -> dict
        kwargs for subprocess.Popen so the child detaches from this process's
        terminal/session. POSIX: start_new_session=True. Windows: DETACHED_PROCESS.
"""
import asyncio, os, re, socket, subprocess, sys, tempfile
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
_TMP = Path(tempfile.gettempdir())
_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _check(name):
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid BU_NAME {name!r}: must match [A-Za-z0-9_-]{{1,64}}")
    return name


def log_path(name):   return _TMP / f"bu-{_check(name)}.log"
def pid_path(name):   return _TMP / f"bu-{_check(name)}.pid"
def port_path(name):  return _TMP / f"bu-{_check(name)}.port"
def _sock_path(name): return _TMP / f"bu-{_check(name)}.sock"


def sock_addr(name):
    """Display-only endpoint identifier. POSIX returns the socket path;
    Windows returns "127.0.0.1:<port>" if the daemon is running, else
    the bare pipe-equivalent placeholder for log lines."""
    if not IS_WINDOWS:
        return str(_sock_path(name))
    try: return f"127.0.0.1:{port_path(name).read_text().strip()}"
    except FileNotFoundError: return f"tcp:bu-{_check(name)}"


def spawn_kwargs():
    if IS_WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# --- client ------------------------------------------------------------------

def connect(name, timeout=1.0):
    """Open a blocking client connection. Returns a connected socket.
    Raises FileNotFoundError if the daemon isn't listening, TimeoutError on
    connect timeout."""
    if not IS_WINDOWS:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout); s.connect(str(_sock_path(name))); return s

    # Windows: read port from sidecar file, then TCP-connect to 127.0.0.1.
    try:
        port = int(port_path(name).read_text().strip())
    except (FileNotFoundError, ValueError):
        raise FileNotFoundError(str(port_path(name)))
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.settimeout(timeout)
    return s


# --- server ------------------------------------------------------------------

async def serve(name, handler):
    """Run the daemon server until cancelled. `handler(reader, writer)` is the
    standard asyncio stream callback; transport is invisible to it."""
    if not IS_WINDOWS:
        path = str(_sock_path(name))
        if os.path.exists(path): os.unlink(path)
        server = await asyncio.start_unix_server(handler, path=path)
        os.chmod(path, 0o600)
        async with server:
            await asyncio.Event().wait()
        return

    # Windows: bind ephemeral port on 127.0.0.1, write it to the .port file
    # so clients can find us. Cleanup on exit.
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    pf = port_path(name)
    pf.write_text(str(port))
    try:
        async with server:
            await asyncio.Event().wait()
    finally:
        try: pf.unlink()
        except FileNotFoundError: pass


def cleanup_endpoint(name):
    """Remove the daemon's endpoint file. POSIX: unix socket. Windows: .port
    sidecar. Best-effort -- silent if already gone."""
    p = _sock_path(name) if not IS_WINDOWS else port_path(name)
    try: p.unlink()
    except FileNotFoundError: pass
