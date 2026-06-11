"""Table model and time rendering for Task Deck.

Rendering lives here (not in systemd_client) so the client remains a faithful
transcription of systemd's µs epochs and the same fixtures drive both layers.
"""
from __future__ import annotations

from datetime import datetime


def format_delta(seconds: float) -> str:
    """Render a positive duration with two significant units, DSM-style.

    Unit pairs chosen for glanceability: seconds alone under a minute, then
    m / h+m / d+h, then bare days past ~5 weeks where hours are noise.
    """
    # Callers must pass non-negative durations (format_when negates past
    # deltas before calling). A negative here is a caller bug — fail loudly
    # at dev time rather than rendering "-345600s" in the table.
    assert seconds >= 0, "format_delta requires non-negative seconds"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        hours, rem = divmod(s, 3600)
        return f"{hours}h {rem // 60}m"
    days, rem = divmod(s, 86400)
    if days >= 35:
        return f"{days}d"
    return f"{days}d {rem // 3600}h"


def format_when(ts_usec: int | None, now: datetime) -> str:
    """Render a µs epoch as 'absolute (relative)' — e.g. 'today 23:10 (in 4h 10m)'.

    `now` is a parameter, not datetime.now(), so tests are deterministic and
    the table can render one consistent instant per refresh. Absolute form
    scales with distance: today / weekday within 6 days / 'Mon DD' beyond.
    """
    if ts_usec is None:
        return "—"
    dt = datetime.fromtimestamp(ts_usec / 1_000_000)
    # Epoch-space subtraction, not (dt - now): both datetimes are naive LOCAL,
    # and wall-clock subtraction goes off by ±1h when the interval spans a DST
    # transition. now.timestamp() interprets naive-local consistently.
    delta = ts_usec / 1_000_000 - now.timestamp()
    if dt.date() == now.date():
        absolute = f"today {dt:%H:%M}"
    elif abs(delta) < 6 * 86400:
        absolute = f"{dt:%a %H:%M}"
    else:
        absolute = f"{dt:%b %d %H:%M}"
    relative = f"in {format_delta(delta)}" if delta >= 0 else f"{format_delta(-delta)} ago"
    return f"{absolute} ({relative})"
