"""Правила реестра: регистрация, поиск, исходы, продление, сборка мусора."""
from __future__ import annotations

import json
import os
import unittest

from regd.registry import EXIT_OK, EXIT_REFUSED, EXIT_USAGE
from regd.tools import FakeTool
from tests.helpers import ToolCase


class RegisterRulesTest(ToolCase):
    def test_registration_without_handshake_leaves_no_record(self):
        registry = self.make_registry()
        tool = self.make_tool(echo_mode="wrong")
        verdict = registry.register(tool.name, tool.base, tool.verbs, "алиса", 60, timeout=1.0)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["exit"], EXIT_REFUSED)
        self.assertEqual(verdict["reason"], "challenge не подтверждён")
        self.assertEqual(registry.store.tools, {})                    # запись не создана
        self.assertFalse(os.path.exists(self.store_path) and
                         json.load(open(self.store_path, encoding="utf-8")).get("tools"))

    def test_registration_records_provenance_of_handshake(self):
        registry = self.make_registry()
        tool = self.make_tool()
        verdict = registry.register(tool.name, tool.base, tool.verbs, "алиса", 60, timeout=1.0)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["exit"], EXIT_OK)
        row = verdict["tool"]
        self.assertEqual(row["name"], tool.name)
        self.assertEqual(row["owner"], "алиса")
        self.assertEqual(row["verbs"], tool.verbs)
        self.assertEqual(row["lease_seconds"], 60)
        self.assertEqual(row["alive"], True)
        stored = registry.store.get(verdict["id"])
        self.assertEqual(stored["last_handshake"]["way"], "поле challenge в JSON")

    def test_registration_requires_usable_input(self):
        registry = self.make_registry()
        tool = self.make_tool()
        cases = [
            {"name": "", "endpoint": tool.base, "verbs": ["pdf.summarize"]},
            {"name": "tool", "endpoint": "не-url", "verbs": ["pdf.summarize"]},
            {"name": "tool", "endpoint": tool.base, "verbs": []},
            {"name": "tool", "endpoint": tool.base, "verbs": ["PDF Summarize!"]},
        ]
        for case in cases:
            verdict = registry.register(case["name"], case["endpoint"], case["verbs"], "боб", 60,
                                        timeout=1.0)
            self.assertEqual(verdict["exit"], EXIT_USAGE, case)
            self.assertFalse(verdict["ok"], case)
        self.assertEqual(registry.store.tools, {})

    def test_failed_reregistration_marks_degraded_but_keeps_old_verbs(self):
        registry = self.make_registry()
        tool = self.make_tool()
        first = registry.register(tool.name, tool.base, ["pdf.summarize", "pdf.extract"], "алиса", 60,
                                  timeout=1.0)
        self.assertTrue(first["ok"])
        tool.echo_mode = "wrong"                                     # инструмент начал врать
        second = registry.register(tool.name, tool.base, ["pdf.merge"], "алиса", 300, timeout=1.0)
        self.assertFalse(second["ok"])
        self.assertTrue(second["recorded"])
        row = registry.store.get(first["id"])
        self.assertEqual(row["verbs"], ["pdf.summarize", "pdf.extract"])   # старые возможности целы
        self.assertEqual(row["lease_seconds"], 60)                         # аренда не продлена
        self.assertTrue(row["degraded"])
        self.assertIn("рукопожатие не подтверждено", row["degraded_reasons"][0])

    def test_failures_are_written_to_journal(self):
        registry = self.make_registry()
        tool = self.make_tool(echo_mode="wrong")
        registry.register(tool.name, tool.base, tool.verbs, "алиса", 60, timeout=1.0)
        with open(self.journal_path, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(records[-1]["action"], "register")
        self.assertFalse(records[-1]["ok"])
        self.assertIn("записи нет", records[-1]["note"])


class FindTest(ToolCase):
    def setUp(self):
        super().setUp()
        self.registry = self.make_registry()
        self.summarizer = self.make_tool(name="pdf-summarizer",
                                         verbs=["pdf.summarize", "pdf.extract"])
        self.extractor = self.make_tool(name="pdf-extract", verbs=["pdf.extract"])
        self.transcriber = self.make_tool(name="audio", verbs=["audio.transcribe"])
        for tool, lease in ((self.summarizer, 600), (self.extractor, 600), (self.transcriber, 600)):
            self.assertTrue(self.register_tool(self.registry, tool, lease)["ok"])

    def test_find_by_exact_verb(self):
        verdict = self.registry.find("pdf.summarize")
        self.assertTrue(verdict["ok"])
        self.assertEqual([row["name"] for row in verdict["matches"]], ["pdf-summarizer"])

    def test_find_by_glob(self):
        verdict = self.registry.find("pdf.*")
        self.assertEqual({row["name"] for row in verdict["matches"]},
                         {"pdf-summarizer", "pdf-extract"})

    def test_find_without_verb_returns_everything(self):
        verdict = self.registry.find()
        self.assertEqual(verdict["count"], 3)

    def test_find_unknown_verb_returns_empty_list(self):
        verdict = self.registry.find("video.render")
        self.assertEqual(verdict["count"], 0)
        self.assertEqual(verdict["matches"], [])

    def test_find_is_ordered_by_reputation(self):
        for _ in range(5):
            self.registry.report(self.summarizer.name, True)
        verdict = self.registry.find("pdf.*")
        self.assertEqual(verdict["matches"][0]["name"], "pdf-summarizer")
        self.assertGreater(verdict["matches"][0]["reputation"],
                           verdict["matches"][-1]["reputation"])

    def test_unavailable_tool_is_marked_not_hidden(self):
        self.extractor.stop()
        verdict = self.registry.find("pdf.*", probe=True)
        rows = {row["name"]: row for row in verdict["matches"]}
        self.assertEqual(len(rows), 2)                                # из выдачи не пропал
        self.assertFalse(rows["pdf-extract"]["alive"])
        self.assertTrue(rows["pdf-summarizer"]["alive"])
        self.assertIn("недоступен", rows["pdf-extract"]["probe_error"])

    def test_lapsed_record_still_visible_until_gc_and_hidden_by_live_flag(self):
        self.registry.store.resolve(self.extractor.name)["expires_at"] = self.clock.now - 1
        rows = self.registry.find("pdf.*", probe=False)["matches"]
        lapsed = [row for row in rows if row["name"] == "pdf-extract"]
        self.assertEqual(len(lapsed), 1)
        self.assertTrue(lapsed[0]["lease_expired"])
        live = self.registry.find("pdf.*", probe=False, live_only=True)["matches"]
        self.assertEqual([row["name"] for row in live], ["pdf-summarizer"])

    def test_probe_can_be_skipped(self):
        self.extractor.stop()
        skipped = self.registry.find("pdf.*", probe=False)["matches"]
        self.assertFalse(skipped[0]["last_probe"] is None)      # без проверки живости никто не дёргался
        worried = self.registry.find("pdf.*", probe=True)
        self.assertFalse(worried["matches"][1]["alive"])        # с проверкой инструмент помечен
        self.assertTrue(worried["matches"][0]["alive"])


class ReportTest(ToolCase):
    def setUp(self):
        super().setUp()
        self.registry = self.make_registry()
        self.tool = self.make_tool()
        self.entry = self.register_tool(self.registry, self.tool)

    def test_report_counts_outcomes(self):
        for ok in (True, False, False):
            verdict = self.registry.report(self.entry["id"], ok, "проба")
            self.assertTrue(verdict["ok"])
        row = verdict["tool"]
        self.assertEqual((row["ok"], row["fail"]), (1, 2))
        self.assertAlmostEqual(row["success_ratio"], 0.333, places=3)
        self.assertEqual(row["last_error"], "проба")

    def test_report_by_name(self):
        verdict = self.registry.report(self.tool.name, True, "по имени")
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["tool"]["ok"], 1)

    def test_report_of_unknown_id_is_refused(self):
        verdict = self.registry.report("нет-такого", True)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["exit"], EXIT_REFUSED)
        self.assertIn("записи", verdict["reason"])

    def test_report_after_gc_points_to_archive(self):
        self.clock.tick(4000)
        self.registry.gc()
        verdict = self.registry.report(self.entry["id"], True)
        self.assertFalse(verdict["ok"])
        self.assertIn("архиве", verdict["reason"])


class RenewTest(ToolCase):
    def setUp(self):
        super().setUp()
        self.registry = self.make_registry()
        self.tool = self.make_tool()
        self.entry = self.register_tool(self.registry, self.tool, lease=60)

    def test_renew_extends_lease_after_fresh_handshake(self):
        before = self.entry["tool"]["expires_at"]
        verdict = self.registry.renew(self.entry["id"], 600, timeout=1.0)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["tool"]["lease_seconds"], 600)
        self.assertGreater(verdict["tool"]["expires_at"], before)
        self.assertEqual(len(self.tool.handshake_payloads()), 2)          # рукопожатие повторилось
        self.assertEqual(self.tool.handshake_payloads()[-1]["lease_seconds"], 600)

    def test_renew_with_dead_tool_is_refused_and_marked(self):
        self.tool.stop()
        verdict = self.registry.renew(self.entry["id"], 600, timeout=1.0)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["exit"], EXIT_REFUSED)
        self.assertIn("не продлена", verdict["reason"])
        row = self.registry.store.get(self.entry["id"])
        self.assertEqual(row["lease_seconds"], 60)                        # аренда не тронута
        self.assertTrue(row["degraded"])
        self.assertFalse(row["alive"])

    def test_renew_keeps_lease_when_not_given(self):
        verdict = self.registry.renew(self.entry["id"], None, timeout=1.0)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["tool"]["lease_seconds"], 60)

    def test_renew_unknown_id_is_refused(self):
        verdict = self.registry.renew("нет-такого", 60, timeout=1.0)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["exit"], EXIT_REFUSED)


class GcTest(ToolCase):
    def test_gc_moves_only_lapsed_and_reports_them(self):
        registry = self.make_registry()
        live = self.make_tool(name="live")
        dying = self.make_tool(name="dying")
        live_entry = self.register_tool(registry, live, lease=3600)
        dying_entry = self.register_tool(registry, dying, lease=10)
        self.clock.tick(11)
        died = registry.gc()
        self.assertEqual(died["moved"], 1)
        self.assertEqual([entry["name"] for entry in died["expired"]], ["dying"])
        self.assertEqual(died["archive_size"], 1)
        self.assertIn(live_entry["id"], registry.store.tools)
        self.assertNotIn(dying_entry["id"], registry.store.tools)
        archive = registry.archive()
        self.assertEqual(archive[0]["id"], dying_entry["id"])
        self.assertIn("аренда истекла", archive[0]["expired_reason"])
        self.assertTrue(archive[0]["expired_at_iso"])
        self.assertEqual(registry.gc()["moved"], 0)                        # повторный прогон пуст

    def test_health_counts_states(self):
        registry = self.make_registry()
        live = self.make_tool(name="live")
        dying = self.make_tool(name="dying")
        self.register_tool(registry, live, lease=3600)
        self.register_tool(registry, dying, lease=10)
        self.clock.tick(11)
        health = registry.health()
        self.assertEqual(health["tools"], 2)
        self.assertEqual(health["live"], 1)
        self.assertEqual(health["lapsed"], 1)
        registry.gc()
        health = registry.health()
        self.assertEqual(health["tools"], 1)
        self.assertEqual(health["expired"], 1)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
