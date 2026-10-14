"""CLI: те же правила из командной строки, машинам — JSON, человеку — строки."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.request

from regd.__main__ import main
from regd.store import Store
from tests.helpers import ToolCase

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliCase(ToolCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--store", self.store_path, "--journal", self.journal_path, *argv])
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv):
        code, out, err = self.run_cli("--json", *argv)
        return code, (json.loads(out) if out.strip() else {}), err

    def stored_tools(self):
        return Store(self.store_path, clock=self.clock).tools

    def expire_lease(self, seconds_ago: float = 1.0):
        """Аренда истекла: CLI считает время по настоящим часам, поэтому правим хранилище."""
        store = Store(self.store_path, clock=time.time)
        for entry in store.tools.values():
            entry["expires_at"] = time.time() - seconds_ago
        store.save()


class RegisterCliTest(CliCase):
    def test_register_ok_json_and_text(self):
        tool = self.make_tool()
        code, payload, err = self.run_json("register", "--name", tool.name,
                                           "--endpoint", tool.base, "--verb", "pdf.summarize",
                                           "--verb", "pdf.extract", "--owner", "алиса",
                                           "--lease", "120", "--timeout", "1")
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tool"]["verbs"], ["pdf.summarize", "pdf.extract"])
        self.assertEqual(payload["tool"]["lease_seconds"], 120)
        self.assertEqual(payload["handshake"]["way"], "поле challenge в JSON")

        code, out, err = self.run_cli("register", "--name", tool.name, "--endpoint", tool.base,
                                      "--verb", "pdf.summarize", "--timeout", "1")
        self.assertEqual(code, 0)
        self.assertIn("зарегистрирован", out)
        self.assertIn("рукопожатие подтверждено", out)

    def test_register_refused_without_handshake(self):
        tool = self.make_tool(echo_mode="wrong")
        code, payload, err = self.run_json("register", "--name", tool.name,
                                           "--endpoint", tool.base, "--verb", "pdf.summarize",
                                           "--timeout", "1")
        self.assertEqual(code, 3)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "challenge не подтверждён")
        self.assertEqual(self.stored_tools(), {})          # записи нет

    def test_register_refused_says_so_in_human_output(self):
        tool = self.make_tool(echo_mode="silent")
        code, out, err = self.run_cli("register", "--name", tool.name, "--endpoint", tool.base,
                                      "--verb", "pdf.summarize", "--timeout", "1")
        self.assertEqual(code, 3)
        self.assertIn("ОТКАЗ", err)
        self.assertIn("записи нет", err)

    def test_register_bad_verb_is_usage_error(self):
        tool = self.make_tool()
        code, payload, err = self.run_json("register", "--name", tool.name,
                                           "--endpoint", tool.base, "--verb", "PDF Summarize!",
                                           "--timeout", "1")
        self.assertEqual(code, 2)
        self.assertIn("некорректные возможности", payload["reason"])

    def test_register_without_verb_is_argparse_error(self):
        tool = self.make_tool()
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["register", "--name", tool.name, "--endpoint", tool.base])
        self.assertEqual(caught.exception.code, 2)


class FindListCliTest(CliCase):
    def setUp(self):
        super().setUp()
        self.summarizer = self.make_tool(name="pdf-summarizer",
                                         verbs=["pdf.summarize", "pdf.extract"])
        self.audio = self.make_tool(name="audio", verbs=["audio.transcribe"])
        for tool in (self.summarizer, self.audio):
            code, _, err = self.run_cli("register", "--name", tool.name, "--endpoint", tool.base,
                                        "--verb", tool.verbs[0], "--timeout", "1")
            self.assertEqual(code, 0, err)

    def test_find_json(self):
        code, payload, err = self.run_json("find", "--verb", "pdf.summarize", "--timeout", "1")
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["matches"][0]["name"], "pdf-summarizer")
        self.assertTrue(payload["matches"][0]["alive"])

    def test_find_glob_and_empty(self):
        code, payload, _ = self.run_json("find", "--verb", "pdf.*", "--timeout", "1")
        self.assertEqual(payload["count"], 1)
        code, payload, _ = self.run_json("find", "--verb", "video.render", "--no-probe")
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 0)

    def test_find_human_output_mentions_reputation_number(self):
        code, out, err = self.run_cli("find", "--verb", "pdf.summarize", "--timeout", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("репутация", out)
        self.assertIn("pdf-summarizer", out)

    def test_list_json_and_text(self):
        code, payload, err = self.run_json("list", "--timeout", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["expired_count"], 0)
        self.assertEqual(payload["live"], 2)

        code, out, err = self.run_cli("list", "--timeout", "1")
        self.assertEqual(code, 0)
        self.assertIn("записей: 2", out)
        self.assertIn("архив expired: 0", out)

    def test_list_marks_dead_tool(self):
        self.audio.stop()
        code, payload, _ = self.run_json("list")
        rows = {row["name"]: row for row in payload["tools"]}
        self.assertFalse(rows["audio"]["alive"])
        self.assertTrue(rows["pdf-summarizer"]["alive"])

    def test_list_hides_archived_records_from_tools(self):
        self.expire_lease()
        code, payload, _ = self.run_json("gc")
        self.assertEqual(code, 0)
        self.assertEqual(payload["moved"], 2)
        code, payload, _ = self.run_json("list")
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["expired_count"], 2)


class ReportRenewCliTest(CliCase):
    def setUp(self):
        super().setUp()
        self.tool = self.make_tool()
        code, payload, err = self.run_json("register", "--name", self.tool.name,
                                           "--endpoint", self.tool.base, "--verb", "pdf.summarize",
                                           "--lease", "60", "--timeout", "1")
        self.assertEqual(code, 0, err)
        self.entry_id = payload["id"]

    def test_report_ok_and_fail(self):
        code, payload, err = self.run_json("report", "--id", self.entry_id, "--ok")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["tool"]["ok"], 1)
        code, payload, err = self.run_json("report", "--id", self.tool.name, "--fail",
                                           "--note", "таймаут")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["tool"]["fail"], 1)
        self.assertEqual(payload["tool"]["last_error"], "таймаут")

    def test_report_text_output(self):
        code, out, err = self.run_cli("report", "--id", self.entry_id, "--ok")
        self.assertEqual(code, 0)
        self.assertIn("исход принят", out)
        self.assertIn("репутация", out)

    def test_report_requires_ok_or_fail(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["--store", self.store_path, "report", "--id", self.entry_id])
        self.assertEqual(caught.exception.code, 2)

    def test_report_unknown_id(self):
        code, payload, _ = self.run_json("report", "--id", "нет-такого", "--ok")
        self.assertEqual(code, 3)
        self.assertIn("записи", payload["reason"])

    def test_renew_extends_and_is_confirmed(self):
        code, payload, err = self.run_json("renew", "--id", self.entry_id, "--lease", "300",
                                           "--timeout", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["tool"]["lease_seconds"], 300)
        self.assertEqual(len(self.tool.handshake_payloads()), 2)

    def test_renew_dead_tool_refused(self):
        self.tool.stop()
        code, payload, err = self.run_json("renew", "--id", self.entry_id, "--timeout", "1")
        self.assertEqual(code, 3)
        self.assertIn("не продлена", payload["reason"])
        code, out, err = self.run_cli("renew", "--id", self.entry_id, "--timeout", "1")
        self.assertEqual(code, 3)
        self.assertIn("ОТКАЗ", err)

    def test_gc_text_shows_reason_and_date(self):
        self.expire_lease()
        code, out, err = self.run_cli("gc")
        self.assertEqual(code, 0, err)
        self.assertIn("перенесено в expired: 1", out)
        self.assertIn("причина: аренда истекла", out)
        self.assertIn("в архиве с", out)


class ServeCommandTest(CliCase):
    def test_serve_command_answers_healthz(self):
        """Реальный процесс `python3 -m regd serve` и реальный HTTP-запрос к нему."""
        process = subprocess.Popen(
            [sys.executable, "-m", "regd", "--store", self.store_path,
             "--journal", self.journal_path, "serve", "--port", "0"],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            line = process.stdout.readline()
            self.assertIn("regd слушает", line)
            port = int(line.strip().rsplit(":", 1)[1])
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["tools"], 0)
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
