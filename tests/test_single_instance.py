"""SingleInstance guard tests — hermetic IPC (no display).

QLocalServer/QLocalSocket are local-socket IPC, not GUI, so these run headless.
Each test uses a unique key (uuid) so a leftover/concurrent socket can't collide,
and removes the socket name afterward regardless of outcome.
"""
import uuid

import pytest
from PySide6.QtNetwork import QLocalServer

from taskdeck.single_instance import SingleInstance


@pytest.fixture
def key():
    k = f"taskdeck-test-{uuid.uuid4()}"
    yield k
    QLocalServer.removeServer(k)


def test_first_instance_is_primary(qtbot, key):
    inst = SingleInstance(key=key)
    assert inst.is_secondary() is False  # nothing else listening → we own it


def test_second_instance_is_detected(qtbot, key):
    primary = SingleInstance(key=key)  # listens from construction
    assert not primary.is_secondary()
    secondary = SingleInstance(key=key)
    assert secondary.is_secondary() is True  # the primary's socket answered


def test_secondary_ping_shows_primary_window(qtbot, key):
    primary = SingleInstance(key=key)
    shown = []
    primary.set_window_shower(lambda: shown.append(1))
    secondary = SingleInstance(key=key)
    assert secondary.is_secondary()
    secondary.ping_primary()
    # The ping is delivered on a later event-loop turn → spin until it lands.
    qtbot.waitUntil(lambda: shown == [1], timeout=2000)


def test_ping_before_shower_is_set_does_not_crash(qtbot, key):
    # Defensive: a ping arriving before the primary has registered its shower
    # must be drained harmlessly (no callback yet), not raise.
    primary = SingleInstance(key=key)  # primary, no shower set yet
    assert not primary.is_secondary()  # (and keeps it listening through the ping)
    secondary = SingleInstance(key=key)
    secondary.ping_primary()
    qtbot.wait(50)  # let the connection be processed; nothing should blow up


def test_prior_instance_shutdown_does_not_block_a_fresh_primary(qtbot, key):
    # After a prior instance's server is gone, a new instance must become a
    # WORKING primary (listening), not be falsely seen as secondary and not fail
    # open as a deaf primary.
    from PySide6.QtNetwork import QLocalSocket

    stale = QLocalServer()
    stale.listen(key)
    stale.close()  # prior instance stops serving
    inst = SingleInstance(key=key)
    assert inst.is_secondary() is False
    # Stronger than "not secondary": confirm it is actually LISTENING (a fresh
    # probe connects) — i.e. listen() succeeded, so it's a real primary that will
    # surface its window on a future launch, not a fail-open deaf one.
    checker = QLocalSocket()
    checker.connectToServer(key)
    assert checker.waitForConnected(500)
    checker.disconnectFromServer()
    checker.close()
