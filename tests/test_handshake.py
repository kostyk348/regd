"""Рукопожатие и живость: инструмент обязан подтвердить challenge, иначе это не доказательство."""
from __future__ import annotations

import unittest

from regd import handshake as hs
from regd.model import CHALLENGE_HEADER, HANDSHAKE_PATH
from tests.helpers import ToolCase


class HandshakeTest(ToolCase):
    def test_json_field_confirms(self):
        tool = self.make_tool()
        verdict = hs.verify_handshake(tool.base, tool.name, tool.verbs, 60, timeout=1.0)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["way"], "поле challenge в JSON")
        self.assertEqual(verdict["reason"], "")

    def test_header_confirms(self):
        tool = self.make_tool(echo_mode="header")
        verdict = hs.verify_handshake(tool.base, tool.name, tool.verbs, 60, timeout=1.0)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["way"], "заголовок %s" % CHALLENGE_HEADER)

    def test_body_echo_confirms(self):
        tool = self.make_tool(echo_mode="body")
        verdict = hs.verify_handshake(tool.base, tool.name, tool.verbs, 60, timeout=1.0)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["way"], "эхо телом")

    def test_wrong_challenge_is_not_confirmed(self):
        tool = self.make_tool(echo_mode="wrong")
        verdict = hs.verify_handshake(tool.base, tool.name, tool.verbs, 60, timeout=1.0)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "challenge не подтверждён")
        self.assertFalse(verdict["way"])

    def test_silent_tool_is_not_confirmed(self):
        tool = self.make_tool(echo_mode="silent")
        verdict = hs.verify_handshake(tool.base, tool.name, tool.verbs, 60, timeout=1.0)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "challenge не подтверждён")

    def test_error_status_is_reported(self):
        tool = self.make_tool(echo_mode="error")
        verdict = hs.verify_handshake(tool.base, tool.name, tool.verbs, 60, timeout=1.0)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "endpoint ответил 500")
        self.assertEqual(verdict["status"], 500)

    def test_dead_endpoint_is_reported_not_raised(self):
        tool = self.make_tool()
        base = tool.base
        tool.stop()
        verdict = hs.verify_handshake(base, tool.name, tool.verbs, 60, timeout=1.0)
        self.assertFalse(verdict["ok"])
        self.assertIn("endpoint недоступен", verdict["reason"])

    def test_unknown_path_answers_404(self):
        tool = self.make_tool()
        verdict = hs.verify_handshake(tool.base + "/wrong", tool.name, tool.verbs, 60, timeout=1.0)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "endpoint ответил 404")

    def test_challenge_is_sent_and_random(self):
        tool = self.make_tool()
        hs.verify_handshake(tool.base, tool.name, ["pdf.summarize"], 60, timeout=1.0)
        hs.verify_handshake(tool.base, tool.name, ["pdf.summarize"], 60, timeout=1.0)
        payloads = tool.handshake_payloads()
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["regd"], "handshake")
        self.assertEqual(payloads[0]["name"], tool.name)
        self.assertEqual(payloads[0]["verbs"], ["pdf.summarize"])
        self.assertEqual(payloads[0]["lease_seconds"], 60)
        self.assertNotEqual(payloads[0]["challenge"], payloads[1]["challenge"])
        self.assertEqual(len(payloads[0]["challenge"]), 32)

    def test_handshake_uses_given_challenge(self):
        tool = self.make_tool()
        verdict = hs.verify_handshake(tool.base, tool.name, tool.verbs, 60, timeout=1.0,
                                     challenge="фиксированный-challenge")
        self.assertTrue(verdict["ok"])
        self.assertEqual(tool.handshake_payloads()[0]["challenge"], "фиксированный-challenge")


class ProbeTest(ToolCase):
    def test_healthy_tool_is_alive(self):
        tool = self.make_tool()
        verdict = hs.probe_alive(tool.base, timeout=1.0)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["status"], 200)

    def test_unhealthy_tool_is_not_alive(self):
        tool = self.make_tool(health_ok=False)
        verdict = hs.probe_alive(tool.base, timeout=1.0)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["status"], 503)
        self.assertIn("503", verdict["reason"])

    def test_stopped_tool_is_not_alive(self):
        tool = self.make_tool()
        base = tool.base
        tool.stop()
        verdict = hs.probe_alive(base, timeout=1.0)
        self.assertFalse(verdict["ok"])
        self.assertIn("недоступен", verdict["reason"])

    def test_health_path_is_what_registry_calls(self):
        tool = self.make_tool()
        hs.probe_alive(tool.base, timeout=1.0)
        self.assertEqual(tool.requests[-1]["path"], "/regd/health")


class EchoParserTest(unittest.TestCase):
    def test_header_beats_body(self):
        ok, way = hs.echoed_challenge("", "abc", "abc")
        self.assertTrue(ok)
        self.assertIn("заголовок", way)

    def test_json_string_without_challenge_is_refused(self):
        ok, reason = hs.echoed_challenge('{"challenge": "другое"}', "", "abc")
        self.assertFalse(ok)
        self.assertEqual(reason, "challenge не подтверждён")

    def test_plain_text_body_echo(self):
        ok, way = hs.echoed_challenge("  abc \n", "", "abc")
        self.assertTrue(ok)
        self.assertEqual(way, "эхо телом")

    def test_garbage_body_is_refused(self):
        ok, reason = hs.echoed_challenge("не json вовсе", "", "abc")
        self.assertFalse(ok)
        self.assertIn("не JSON", reason)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
