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
            # Connected → a primary is running. The connection ITSELF surfaces
            # its window (its newConnection fires on connect); keep the probe so
            # main() can release it via ping_primary().
            self._probe = probe
            return
        # Did the probe fail because there's simply no socket file (clean first
        # launch / a peer that hasn't listened yet), vs. a stale file with no
        # listener? (Probed 2026-06-17: no file → ServerNotFoundError.)
        no_socket_file = (
            probe.error() == QLocalSocket.LocalSocketError.ServerNotFoundError
        )
        probe.close()
        # Become primary NOW (before the window is built) so a near-simultaneous
        # second launch sees us. removeServer() is an unconditional unlink, so
        # only clear the name when a stale socket FILE actually exists (probe was
        # refused, not ServerNotFound) — this both reclaims a crashed instance's
        # socket AND avoids unlinking a peer that won a parallel first-launch race
        # and is mid-listen. Residual race (DEF-SI-01): two truly simultaneous
        # FIRST launches can both miss; the loser's listen() then fails and it
        # falls open. Not reachable via autostart-then-manual-launch (never
        # simultaneous).
        if not no_socket_file:
            QLocalServer.removeServer(self._key)
        server = QLocalServer()
        if not server.listen(self._key):
            # Fail open: couldn't bind (rare) → run WITHOUT the guard rather than
            # block the launch. self._server stays None so _on_new_connection
            # no-ops and we never present as a working-but-deaf primary.
            return
        self._server = server
        self._server.newConnection.connect(self._on_new_connection)

    def is_secondary(self) -> bool:
        """True if another instance owns the socket (this process should exit)."""
        return self._secondary

    def ping_primary(self) -> None:
        """Release our probe connection to the primary, then the caller exits.

        NOTE: the connection was opened in the constructor when we detected the
        primary, and THAT connect is what already triggered the primary to
        surface its window (QLocalServer.newConnection fires on connect). Task
        Deck forwards no launch arguments, so there's no payload to send — this
        just closes our end cleanly. (The kernel keeps the accepted connection in
        the primary's listen backlog even after we disconnect/exit, so the
        primary still fires newConnection.)"""
        if self._probe is None:
            return  # not a secondary — nothing to release
        self._probe.disconnectFromServer()
        self._probe.close()

    def set_window_shower(self, shower: Callable[[], None]) -> None:
        """Register the callback the primary runs on each later ping (typically
        'raise the window'). Call only on the primary, after the window exists."""
        self._show = shower

    def _on_new_connection(self) -> None:
        # A later launch connected. Surface the window and release the connection
        # from OUR side too — don't depend on the (possibly already-exited)
        # secondary's disconnect to free it, or it would live for our whole run.
        # We never read a payload; the connection is the whole signal. _show may
        # still be None if a connect arrives before the window is wired (the
        # constructor listens before build()); draining without showing is the
        # correct, harmless behavior in that window.
        if self._server is None:
            return
        conn = self._server.nextPendingConnection()
        if self._show is not None:
            self._show()
        if conn is not None:
            conn.disconnected.connect(conn.deleteLater)
            conn.disconnectFromServer()
