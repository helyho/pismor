from pathlib import Path
import pytest
from pismor.core.db import get_conn, init_schema
from pismor.core.taxonomy import load_taxonomy

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def conn(tmp_path):
    c = get_conn(tmp_path / "test.db")
    init_schema(c, vec_dimensions=None)
    return c


@pytest.fixture
def taxonomy():
    return load_taxonomy(REPO / "config" / "taxonomy.yaml")


class ScriptedLLM:
    """按调用序弹出预设响应的假 LLM（端到端/回归脚本共用机制）。

    responses 元素为响应本体 dict，或 {"schema": name, "response": {...}}：
    后者按本次调用的 schema.name 校验脚本顺序——写错顺序不会被管线静默吞掉
    （错误同时记入 self.errors，供测试在 wait 环节给出明确失败信息）。
    """

    def __init__(self, responses):
        self._queue = list(responses)
        self.calls = []
        self.errors = []

    @property
    def remaining(self):
        return list(self._queue)

    def count(self, schema_name):
        return self.calls.count(schema_name)

    def _pop(self, schema_name):
        if not self._queue:
            err = AssertionError(f"scripted responses exhausted at {schema_name}")
            self.errors.append(err)
            raise err
        item = self._queue.pop(0)
        if isinstance(item, dict) and set(item) == {"schema", "response"}:
            expected = item["schema"]
            if expected != schema_name:
                err = AssertionError(
                    f"scripted response expects {expected}, pipeline called {schema_name}")
                self.errors.append(err)
                raise err
            return item["response"]
        return item

    def chat_json(self, system, user, schema):
        name = schema.get("name")
        self.calls.append(name)
        return self._pop(name)


@pytest.fixture
def fake_llm_scripted():
    """工厂夹具：fake_llm_scripted([...]) → ScriptedLLM。"""
    def _make(responses):
        return ScriptedLLM(responses)
    return _make
