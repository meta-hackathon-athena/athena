import pytest

from memory.db import _client
from memory.store import MemoryStore


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.rdb")


@pytest.fixture
def make_store(db_path):
    stores = []

    def make(user="alice"):
        g = _client(db_path).select_graph(f"veyra_{user}")
        s = MemoryStore(g)
        s.setup()
        stores.append(g)
        return s

    yield make
    for g in stores:
        try:
            g.delete()
        except Exception:
            pass
