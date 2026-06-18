"""Single-instance guard via QLocalServer/QLocalSocket.

Why this exists: the tray + autostart feature means Task Deck can be launched
more than once (autostart at login, then a manual launch), and without a guard
each launch spawns its OWN tray icon + background monitor — the duplicate-tray
bug. This makes the first launch the sole "primary": a later launch connects to
the primary's named socket, tells it to show its window, and exits. So
re-launching a tray-resident app un-hides the existing window instead of
duplicating it.

No widgets here — QLocalServer/QLocalSocket are local-socket IPC, so this module
is pure and testable headlessly (like systemd_client and monitor).
"""
from __future__ import annotations

import getpass
from collections.abc import Callable

from PySide6.QtNetwork import QLocalServer, QLocalSocket


def _default_key() -> str:
    """Per-user socket name. QLocalServer already scopes the socket to the
    user's runtime dir on Linux, but folding in the username avoids any
    cross-user name collision on a shared machine."""
    return f"taskdeck-{getpass.getuser()}"


class SingleInstance:
    """Probe-then-own single-instance coordinator. Construct ONCE at startup.

    If another instance already holds the socket, is_secondary() is True — call
    ping_primary() to surface that instance, then exit this process. Otherwise
    this process becomes the primary immediately (it starts listening in the
    constructor, which keeps the probe→listen race window as small as possible);
    register set_window_shower(fn) once the window exists, and fn() is invoked
    whenever a later launch pings us.
    """

    # A real primary answers a local-socket connect in well under this; if
    # nothing answers within the timeout, there is no primary.
    _CONNECT_TIMEOUT_MS = 200

    def __init__(self, key: str | None = None) -> None:
        self._key = key or _default_key()
        self._server: QLocalServer | None = None
        self._show: Callable[[], None] | None = None
        self._probe: QLocalSocket | None = None

        probe = QLocalSocket()
        probe.connectToServer(self._key)
        self._secondary = bool(probe.waitForConnected(self._CONNECT_TIMEOUT_MS))
        if self._secondary:
            self._probe = probe  # kept for ping_primary()
            return
        probe.close()
        # We are the primary: start listening NOW (before the window is built)
        # so a near-simultaneous second launch sees us. A previous instance that
        # crashed can leave the name bound on Linux — clear it first so listen()
        # succeeds. listen() failing for any other reason is non-fatal: the app
        # simply runs without the guard (fail open, never block the launch).
        QLocalServer.removeServer(self._key)
        self._server = QLocalServer()
        self._server.listen(self._key)
        self._server.newConnection.connect(self._on_new_connection)

    def is_secondary(self) -> bool:
        """True if another instance owns the socket (this process should exit)."""
        return self._secondary

    def ping_primary(self) -> None:
        """Tell the running primary to show its window, then drop the probe. The
        connection itself is the signal; the payload is a courtesy, not required."""
        if self._probe is None:
            return  # not a secondary — nothing to ping
        self._probe.write(b"show")
        self._probe.waitForBytesWritten(self._CONNECT_TIMEOUT_MS)
        self._probe.disconnectFromServer()
        self._probe.close()

    def set_window_shower(self, shower: Callable[[], None]) -> None:
        """Register the callback the primary runs on each later ping (typically
        'raise the window'). Call only on the primary, after the window exists."""
        self._show = shower

    def _on_new_connection(self) -> None:
        # A later launch connected. Drain/clean up the connection and surface the
        # window. We don't read the payload — the connection is the whole signal.
        # _show may still be None if a ping somehow arrives before the window is
        # wired (the constructor listens before build()); draining without
        # showing is the correct, harmless behavior in that window.
        if self._server is None:
            return
        conn = self._server.nextPendingConnection()
        if conn is not None:
            conn.disconnected.connect(conn.deleteLater)
        if self._show is not None:
            self._show()
