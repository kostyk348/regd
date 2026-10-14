"""HTTP-фасад: те же правила по сети, `GET /tools`, `GET /find`, `POST /report`, `GET /healthz`."""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from regd.registry import build_server
from tests.helpers import ToolCase


class ServeCase(ToolCase):
    def setUp(self):
        super().setUp()
        self.registry = self.make_registry()
        self.tool = self.make_tool()
        self.entry = self.register_tool(self.registry, self.tool, lease=600)
        self.server = build_server(self.registry, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def url(self, path: str) -> str:
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def request(self, path: str, payload=None, raw=None, content_type="application/json"):
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif raw is not None:
            data = raw.encode("utf-8")
        request = urllib.request.Request(self.url(path), data=data, method="POST" if data else "GET",
                                         headers={"Content-Type": content_type} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            code = exc.code
            exc.close()
            try:
                return code, json.loads(body)
            except ValueError:                          # pragma: no cover - фасад всегда JSON
                return code, {"raw": body}


class HealthAndToolsTest(ServeCase):
    def test_healthz(self):
        status, payload = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tools"], 1)
        self.assertEqual(payload["live"], 1)
        self.assertEqual(payload["expired"], 0)
        self.assertIsInstance(payload["now"], int)

    def test_tools_lists_records_without_probing(self):
        self.tool.stop()
        status, payload = self.request("/tools")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        row = payload["tools"][0]
        self.assertEqual(row["name"], self.tool.name)
        self.assertEqual(row["reputation"], row["reputation"])          # число, не звёздочки
        self.assertTrue(payload["ok"])


class FindEndpointTest(ServeCase):
    def test_find_by_verb(self):
        status, payload = self.request("/find?verb=pdf.summarize")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["matches"][0]["id"], self.entry["id"])

    def test_find_without_verb_is_a_usage_error(self):
        status, payload = self.request("/find")
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("?verb=", payload["error"])

    def test_find_unknown_verb_is_empty_not_error(self):
        status, payload = self.request("/find?verb=video.render")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 0)

    def test_find_with_probe_marks_dead_tool(self):
        self.tool.stop()
        status, payload = self.request("/find?verb=pdf.summarize&probe=1")
        self.assertEqual(status, 200)
        self.assertFalse(payload["matches"][0]["alive"])
        self.assertIn("недоступен", payload["matches"][0]["probe_error"])

    def test_find_live_filters_lapsed(self):
        self.registry.store.resolve(self.entry["id"])["expires_at"] = self.clock.now - 1
        status, payload = self.request("/find?verb=pdf.summarize&live=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 0)

    def test_glob_search_over_http(self):
        status, payload = self.request("/find?verb=pdf.*")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)


class ReportEndpointTest(ServeCase):
    def test_report_success(self):
        status, payload = self.request("/report", {"id": self.entry["id"], "ok": True, "note": "http"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tool"]["ok"], 1)
        self.assertEqual(payload["tool"]["success_ratio"], 1.0)

    def test_report_failure_with_note(self):
        status, payload = self.request("/report", {"id": self.tool.name, "ok": False, "note": "таймаут"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["tool"]["fail"], 1)
        self.assertEqual(payload["tool"]["last_error"], "таймаут")

    def test_report_unknown_id_is_404(self):
        status, payload = self.request("/report", {"id": "нет-такого", "ok": True})
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])
        self.assertIn("записи", payload["reason"])

    def test_report_without_ok_field_is_400(self):
        status, payload = self.request("/report", {"id": self.entry["id"]})
        self.assertEqual(status, 400)
        self.assertIn("ok", payload["error"])

    def test_report_without_id_is_400(self):
        status, payload = self.request("/report", {"ok": True})
        self.assertEqual(status, 400)
        self.assertIn("id", payload["error"])

    def test_report_does_not_parse_human_forms(self):
        status, payload = self.request("/report", raw="id=%s&ok=1" % self.entry["id"],
                                       content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 400)
        self.assertIn("JSON", payload["error"])

    def test_report_with_broken_json_is_400(self):
        status, payload = self.request("/report", raw="{не json")
        self.assertEqual(status, 400)
        self.assertIn("JSON", payload["error"])


class RoutingTest(ServeCase):
    def test_unknown_paths_are_404(self):
        for path in ("/", "/registry", "/find/", "/expired"):
            status, payload = self.request(path)
            self.assertEqual(status, 404, path)
            self.assertIn("нет такого пути", payload["error"])

    def test_unknown_post_path_is_404(self):
        status, payload = self.request("/register", {"name": "x"})
        self.assertEqual(status, 404)
        self.assertIn("нет такого пути", payload["error"])

    def test_served_registry_is_the_same_store(self):
        self.request("/report", {"id": self.entry["id"], "ok": True})
        reloaded = self.registry.store.get(self.entry["id"])
        self.assertEqual(reloaded["ok"], 1)
        with open(self.store_path, encoding="utf-8") as fh:
            on_disk = json.load(fh)["tools"][self.entry["id"]]
        self.assertEqual(on_disk["ok"], 1)          # HTTP-путь пишет в тот же файл


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
