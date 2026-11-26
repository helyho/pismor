"""运行期配置存储：请求线程读取 current()，配置中心保存后 replace/reload。

Settings/Taxonomy 都是 frozen dataclass；这里只替换引用，避免请求读到半更新对象。
LLM/Embedder 客户端不在这里热重建，连接类字段变更由 UI 提示重启。
"""
from __future__ import annotations

import threading
from pathlib import Path

from pismor.core.config import Settings, load_settings
from pismor.core.taxonomy import Taxonomy, load_taxonomy


class SettingsStore:
    def __init__(self, path: Path | None, initial: Settings):
        self.path = path
        self._settings = initial
        self._lock = threading.RLock()

    def current(self) -> Settings:
        with self._lock:
            return self._settings

    def replace(self, settings: Settings) -> Settings:
        with self._lock:
            self._settings = settings
            if settings.config_path is not None:
                self.path = settings.config_path
            return self._settings

    def reload(self) -> Settings:
        path = self.path
        if path is None:
            raise RuntimeError("settings path is not configured")
        return self.replace(load_settings(path))


class TaxonomyStore:
    def __init__(self, path: Path, initial: Taxonomy):
        self.path = path
        self._taxonomy = initial
        self._lock = threading.RLock()

    def current(self) -> Taxonomy:
        with self._lock:
            return self._taxonomy

    def replace(self, taxonomy: Taxonomy) -> Taxonomy:
        with self._lock:
            self._taxonomy = taxonomy
            if taxonomy.path is not None:
                self.path = taxonomy.path
            return self._taxonomy

    def reload(self) -> Taxonomy:
        return self.replace(load_taxonomy(self.path))
