"""Хранилище: аренда, репутация, архив `expired` и атомарная запись."""
from __future__ import annotations

import json
import os
import unittest

from regd.model import (DEGRADE_AFTER_PROBES, DEFAULT_LEASE, FRESHNESS_HALF_LIFE,
                        freshness, refresh_flags, reputation, success_ratio,
                        tool_id, verb_matches, view)
from tests.helpers import TempDirCase


class RegisterTest(TempDirCase):
    def test_register_creates_entry_and_persists(self):
        store = self.make_store()
        entry = store.register("pdf-summarizer", "http://127.0.0.1:9000",
                               ["pdf.summarize", "PDF.Extract"], "алиса", 60, "поле challenge в JSON")
        self.assertEqual(entry["id"], tool_id("pdf-summarizer", "http://127.0.0.1:9000"))
        self.assertEqual(entry["verbs"], ["pdf.summarize", "pdf.extract"])   # возможности нормализованы
        self.assertEqual(entry["owner"], "алиса")
        self.assertEqual(entry["expires_at"], self.clock.now + 60)
        self.assertFalse(entry["degraded"])
        self.assertTrue(entry["alive"])

        reopened = self.make_store()                 # читаем тот же файл заново
        again = reopened.get(entry["id"])
        self.assertIsNotNone(again)
        self.assertEqual(again["verbs"], ["pdf.summarize", "pdf.extract"])
        self.assertEqual(again["lease_seconds"], 60)

    def test_reregister_keeps_counters_and_extends_lease(self):
        store = self.make_store()
        entry = store.register("tool", "http://127.0.0.1:9001", ["pdf.summarize"], "bob", 60, "json")
        store.note_report(entry["id"], True)
        store.note_report(entry["id"], False, "таймаут")
        self.clock.tick(30)
        again = store.register("tool", "http://127.0.0.1:9001", ["pdf.summarize", "pdf.merge"],
                               "bob", 120, "json")
        self.assertEqual(again["id"], entry["id"])
        self.assertEqual((again["ok"], again["fail"]), (1, 1))       # история исходов не потеряна
        self.assertEqual(again["expires_at"], self.clock.now + 120)
        self.assertEqual(len(store.tools), 1)                        # дубля записи нет

    def test_same_name_on_other_endpoint_is_other_record(self):
        store = self.make_store()
        one = store.register("tool", "http://127.0.0.1:9001", ["pdf.summarize"], "", 60, "json")
        two = store.register("tool", "http://127.0.0.1:9002", ["pdf.summarize"], "", 60, "json")
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(len(store.tools), 2)

    def test_lease_is_clamped(self):
        store = self.make_store()
        entry = store.register("tool", "http://127.0.0.1:9003", ["pdf.summarize"], "", 0, "json")
        self.assertEqual(entry["lease_seconds"], 1)                  # минимум — секунда
        entry = store.register("tool", "http://127.0.0.1:9003", ["pdf.summarize"], "", 10 ** 9, "json")
        self.assertEqual(entry["lease_seconds"], 30 * 24 * 3600)     # максимум — 30 суток

    def test_atomic_save_leaves_no_temp_files(self):
        store = self.make_store()
        store.register("tool", "http://127.0.0.1:9004", ["pdf.summarize"], "", 60, "json")
        names = sorted(os.listdir(self.tmp))
        self.assertEqual(names, ["regd.json"])
        with open(self.store_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["version"], 1)
        self.assertIn("tools", payload)


class ReportTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = self.make_store()
        self.entry = self.store.register("tool", "http://127.0.0.1:9100",
                                         ["pdf.summarize"], "", 600, "json")

    def test_reports_count_and_drive_reputation(self):
        for ok in (True, True, True, False):
            self.store.note_report(self.entry["id"], ok, "проба")
        row = view(self.store.get(self.entry["id"]), self.clock.now)
        self.assertEqual((row["ok"], row["fail"], row["attempts"]), (3, 1, 4))
        self.assertAlmostEqual(row["success_ratio"], 0.75, places=3)
        self.assertAlmostEqual(row["freshness"], 1.0, places=3)
        self.assertAlmostEqual(row["reputation"], 0.75, places=3)

    def test_report_by_name_resolves(self):
        self.store.note_report("tool", True)
        self.assertEqual(self.store.get(self.entry["id"])["ok"], 1)

    def test_report_of_unknown_record_changes_nothing(self):
        self.assertIsNone(self.store.note_report("нет-такого", True))
        self.assertEqual(self.store.get(self.entry["id"])["ok"], 0)

    def test_report_history_is_bounded(self):
        for _ in range(40):
            self.store.note_report(self.entry["id"], True)
        self.assertEqual(len(self.store.get(self.entry["id"])["reports"]), 20)

    def test_reputation_counts_freshness(self):
        row_now = view(self.store.get(self.entry["id"]), self.clock.now)
        later = self.clock.now + FRESHNESS_HALF_LIFE
        row_later = view(self.store.get(self.entry["id"]), later)
        self.assertLess(row_later["reputation"], row_now["reputation"])
        self.assertAlmostEqual(row_later["freshness"], 0.5, places=2)


class GcTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = self.make_store()
        self.live = self.store.register("live-tool", "http://127.0.0.1:9200",
                                        ["pdf.summarize"], "алиса", 3600, "json")
        self.dying = self.store.register("dying-tool", "http://127.0.0.1:9201",
                                         ["pdf.summarize"], "боб", 10, "json")
        self.store.note_report(self.dying["id"], True)
        self.store.note_report(self.dying["id"], False, "таймаут")

    def test_gc_moves_lapsed_to_archive_with_reason_and_date(self):
        self.clock.tick(11)
        moved = self.store.gc()
        self.assertEqual([entry["name"] for entry in moved], ["dying-tool"])
        self.assertNotIn(self.dying["id"], self.store.tools)                # ушла из оборота
        archived = self.store.archived("dying-tool")
        self.assertIsNotNone(archived)                                      # но не удалена
        self.assertEqual(archived["name"], "dying-tool")
        self.assertEqual(archived["verbs"], ["pdf.summarize"])
        self.assertEqual(archived["expired_at"], self.clock.now)
        self.assertIn("аренда истекла", archived["expired_reason"])
        self.assertIn("продления не было", archived["expired_reason"])
        self.assertEqual(archived["history"]["ok"], 1)
        self.assertEqual(archived["history"]["fail"], 1)
        self.assertEqual(archived["history"]["last_error"], "таймаут")

    def test_gc_keeps_live_record(self):
        self.clock.tick(5)
        self.assertEqual(self.store.gc(), [])
        self.assertIn(self.live["id"], self.store.tools)

    def test_gc_is_idempotent_and_archive_grows_once(self):
        self.clock.tick(11)
        self.assertEqual(len(self.store.gc()), 1)
        self.assertEqual(self.store.gc(), [])
        self.assertEqual(len(self.store.expired), 1)

    def test_expired_record_appears_in_journal(self):
        self.clock.tick(11)
        self.store.gc()
        actions = [record["action"] for record in self.store.journal]
        self.assertIn("gc", actions)

    def test_lapsed_row_still_listed_before_gc(self):
        self.clock.tick(11)
        rows = {row["id"]: row for row in self.store.rows()}
        self.assertTrue(rows[self.dying["id"]]["lease_expired"])
        self.assertFalse(rows[self.live["id"]]["lease_expired"])
        self.assertEqual(len(self.store.lapsed_rows()), 1)
        self.assertEqual([row["id"] for row in self.store.rows(live_only=True)], [self.live["id"]])


class FlagsTest(TempDirCase):
    def test_degraded_after_repeated_probe_failures_and_heals(self):
        store = self.make_store()
        entry = store.register("tool", "http://127.0.0.1:9300", ["pdf.summarize"], "", 60, "json")
        for _ in range(DEGRADE_AFTER_PROBES - 1):
            store.note_probe(entry["id"], False, "недоступен")
        self.assertFalse(store.get(entry["id"])["degraded"])
        store.note_probe(entry["id"], False, "недоступен")
        row = view(store.get(entry["id"]), self.clock.now)
        self.assertTrue(row["degraded"])
        self.assertEqual(row["probe_failures"], DEGRADE_AFTER_PROBES)
        store.note_probe(entry["id"], True)                       # инструмент вернулся — метка снята
        row = view(store.get(entry["id"]), self.clock.now)
        self.assertFalse(row["degraded"])
        self.assertEqual(row["probe_failures"], 0)

    def test_degraded_on_failed_handshake_and_cleared_by_renew(self):
        store = self.make_store()
        entry = store.register("tool", "http://127.0.0.1:9301", ["pdf.summarize"], "", 60, "json")
        store.fail_handshake(entry["id"], "challenge не подтверждён")
        row = view(store.get(entry["id"]), self.clock.now)
        self.assertTrue(row["degraded"])
        self.assertIn("рукопожатие", row["degraded_reasons"][0])
        self.assertFalse(row["alive"])
        store.renew(entry["id"], 60, "поле challenge в JSON")
        self.assertFalse(view(store.get(entry["id"]), self.clock.now)["degraded"])

    def test_degraded_only_after_enough_reports(self):
        store = self.make_store()
        entry = store.register("tool", "http://127.0.0.1:9302", ["pdf.summarize"], "", 60, "json")
        for _ in range(4):
            store.note_report(entry["id"], False, "провал")
        self.assertFalse(store.get(entry["id"])["degraded"])       # четырёх исходов мало для приговора
        store.note_report(entry["id"], False, "провал")
        row = view(store.get(entry["id"]), self.clock.now)
        self.assertTrue(row["degraded"])
        self.assertIn("доля успехов", row["degraded_reasons"][0])

    def test_refresh_flags_ignores_probe_error_without_failures(self):
        entry = {"ok": 0, "fail": 0, "probe_failures": 0, "handshake_error": ""}
        refresh_flags(entry)
        self.assertFalse(entry["degraded"])
        self.assertEqual(entry["degraded_reasons"], [])


class ModelTest(unittest.TestCase):
    def test_success_ratio_none_without_attempts(self):
        self.assertIsNone(success_ratio(0, 0))
        self.assertEqual(success_ratio(1, 3), 0.25)

    def test_freshness_decays_half_per_half_life(self):
        self.assertAlmostEqual(freshness(1000.0, 1000.0), 1.0, places=6)
        self.assertAlmostEqual(freshness(1000.0, 1000.0 + FRESHNESS_HALF_LIFE), 0.5, places=6)
        self.assertEqual(freshness(0.0, 1000.0), 0.0)

    def test_neutral_reputation_without_calls(self):
        entry = {"ok": 0, "fail": 0, "created": 1000.0}
        self.assertEqual(reputation(entry, 1000.0), 0.5)

    def test_verb_matches_exact_and_glob(self):
        verbs = ["pdf.summarize", "pdf.extract.text"]
        self.assertTrue(verb_matches("pdf.summarize", verbs))
        self.assertTrue(verb_matches("PDF.Summarize", verbs))
        self.assertTrue(verb_matches("pdf.*", verbs))
        self.assertTrue(verb_matches("pdf.extract.?ext", verbs))
        self.assertFalse(verb_matches("audio.transcribe", verbs))

    def test_tool_id_is_stable_and_endpoint_sensitive(self):
        self.assertEqual(tool_id("a", "http://h:1"), tool_id("a", "http://h:1/"))
        self.assertNotEqual(tool_id("a", "http://h:1"), tool_id("a", "http://h:2"))

    def test_view_marks_lapsed_lease(self):
        entry = {"id": "x", "name": "n", "endpoint": "http://h:1", "verbs": ["a.b"],
                 "created": 100.0, "renewed": 100.0, "expires_at": 110.0, "ok": 0, "fail": 0}
        self.assertFalse(view(entry, 105.0)["lease_expired"])
        self.assertTrue(view(entry, 111.0)["lease_expired"])
        self.assertEqual(view(entry, 111.0)["expires_in"], -1)


if __name__ == "__main__":
    unittest.main()
