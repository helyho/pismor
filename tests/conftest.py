from pathlib import Path
import pytest
from mem.taxonomy import load_taxonomy

REPO = Path(__file__).resolve().parent.parent

@pytest.fixture
def taxonomy():
    return load_taxonomy(REPO / "config" / "taxonomy.yaml")
