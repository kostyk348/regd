"""Общая обвязка тестов: временное хранилище, управляемые часы, фальшивый инструмент."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from regd.model import DEFAULT_LEASE
from regd.registry import Registry
from regd.store import Store
from regd.tools import FakeTool


class Clock:
    """Часы под контролем теста: истёкшую аренду ждать в реальном времени не нужно."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regd-test-")
        self.store_path = os.path.join(self.tmp, "regd.json")
        self.journal_path = os.path.join(self.tmp, "regd-journal.jsonl")
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_store(self, **kwargs) -> Store:
        return Store(self.store_path, clock=self.clock, journal_path=None, **kwargs)

    def make_registry(self, **kwargs) -> Registry:
        return Registry(self.store_path, clock=self.clock, journal_path=self.journal_path, **kwargs)


class ToolCase(TempDirCase):
    """Тесты, которым нужен настоящий endpoint: поднимаем фальшивый инструмент на loopback."""

    tools: list

    def setUp(self):
        super().setUp()
        self.tools = []

    def tearDown(self):
        for tool in self.tools:
            tool.stop()
        super().tearDown()

    def make_tool(self, name: str = "pdf-summarizer", verbs=None, **kwargs) -> FakeTool:
        tool = FakeTool(name=name, verbs=verbs or ["pdf.summarize"], **kwargs)
        self.tools.append(tool)
        return tool.start()

    def register_tool(self, registry: Registry, tool: FakeTool, lease: int = DEFAULT_LEASE,
                      owner: str = "тест", **kwargs) -> dict:
        return registry.register(tool.name, tool.base, tool.verbs, owner=owner,
                                 lease_seconds=lease, timeout=1.0, **kwargs)
