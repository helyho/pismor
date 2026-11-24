from pathlib import Path
import pytest
from mem.db import get_conn, init_schema
from mem.taxonomy import load_taxonomy

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def conn(tmp_path):
    c = get_conn(tmp_path / "test.db")
    init_schema(c, vec_dimensions=None)
    return c


@pytest.fixture
def taxonomy():
    return load_taxonomy(REPO / "config" / "taxonomy.yaml")
