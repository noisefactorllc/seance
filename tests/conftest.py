import time

import pytest


class FakeClock:
    def __init__(self, start: float = 1_751_500_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def real_clock():
    return time.time


class FakeConn:
    """Test double for transport connections."""

    _n = 0

    def __init__(self, identity, connection_id=None, declared_dialects=None):
        type(self)._n += 1
        self.identity = identity
        self.connection_id = connection_id or f"conn-{type(self)._n}"
        self.declared_dialects = declared_dialects
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None

    def send_json(self, msg: dict) -> bool:
        if self.closed:
            return False
        self.sent.append(msg)
        return True

    def close_soon(self, code: int, reason: str = "") -> None:
        self.closed = (code, reason)
