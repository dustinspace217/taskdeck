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


def test_stale_socket_does_not_block_a_fresh_primary(qtbot, key):
    # A crashed primary can leave the name bound on Linux. A new instance must
    # reclaim it (removeServer before listen), not be falsely seen as secondary.
    stale = QLocalServer()
    stale.listen(key)
    stale.close()  # "crash": stop serving; the name may linger
    inst = SingleInstance(key=key)
    assert inst.is_secondary() is False  # reclaimed, not mistaken for secondary
