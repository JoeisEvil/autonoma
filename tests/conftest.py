import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ledger  # noqa: E402


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    ledger.init(conn)
    yield conn
    conn.close()


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()
