"""`regd demo` — доказательство сценария запуском: до и после `gc`."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest

from regd.__main__ import main
from regd.demo import run_demo


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regd-demo-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_demo_shows_before_and_after_gc(self):
        lines: list[str] = []
        summary = run_demo(out=lines.append, workdir=self.tmp, dying_lease=1,
                           sleep=time.sleep, probe_timeout=1.0)
        text = "\n".join(lines)

        self.assertTrue(summary["ok"], text)
        self.assertEqual(summary["before_gc"], 2)
        self.assertEqual(summary["lapsed_before_gc"], 1)
        self.assertEqual(summary["after_gc"], 1)
        self.assertEqual(summary["expired"], ["legacy-summarizer"])
        self.assertIs(summary["deaf_recorded"], False)
        self.assertTrue(summary["renewed"])

        for marker in ("challenge не подтверждён",      # пункт 1 MVP: без рукопожатия записи нет
                       "записи нет",
                       "выдача ДО `gc`",                  # пункт 5: до сборки мусора запись видна
                       "выдача ПОСЛЕ `gc`",               # ...и после сборки уходит
                       "причина: аренда истекла",         # в архив попадает с причиной и датой
                       "сценарий сходится"):
            self.assertIn(marker, text)

    def test_demo_writes_journal(self):
        run_demo(out=lambda line: None, workdir=self.tmp, dying_lease=1, probe_timeout=1.0)
        with open(os.path.join(self.tmp, "regd-journal.jsonl"), encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        actions = [record["action"] for record in records]
        self.assertIn("register", actions)
        self.assertIn("report", actions)
        self.assertIn("renew", actions)
        self.assertIn("gc", actions)
        failed = [record for record in records if record["action"] == "register" and not record["ok"]]
        self.assertEqual(len(failed), 1)                # глухой инструмент отмечен в журнале

    def test_demo_via_cli_returns_zero(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["demo", "--workdir", self.tmp, "--dying-lease", "1"])
        self.assertEqual(code, 0)
        self.assertIn("сценарий сходится", out.getvalue())


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
