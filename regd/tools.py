"""Фальшивые инструменты для `regd demo` и тестов.

Это те самые endpoint'ы, которые реестр обязан проверить: один честно возвращает
challenge, второй умирает («гасим»), третий молчит — и в реестр не попадает вовсе.
Инструмент здесь настоящий HTTP-сервер на loopback, а не заглушка: проверка идёт
по сети, как пойдёт к живому MCP-инструменту.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .model import CHALLENGE_HEADER, HANDSHAKE_PATH, HEALTH_PATH

MAX_BODY = 64 * 1024
ECHO_MODES = ("json", "header", "body", "wrong", "silent", "error")


class _ToolHandler(BaseHTTPRequestHandler):
    tool: "FakeTool" = None          # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _send_json(self, code: int, payload: dict, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str, extra: dict | None = None) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = min(int(self.headers.get("Content-Length", "0") or 0), MAX_BODY)
        raw = self.rfile.read(length)
        self.tool.requests.append({"path": path, "body": raw})
        if path != HANDSHAKE_PATH:
            self._send_json(404, {"error": "нет такого пути"})
            return
        if self.tool.handshake_delay:
            time.sleep(self.tool.handshake_delay)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        challenge = str(payload.get("challenge") or "")
        mode = self.tool.echo_mode
        if mode == "error":
            self._send_json(500, {"error": "инструмент сломан"})
        elif mode == "wrong":
            self._send_json(200, {"challenge": challenge + "-нет", "ok": True})
        elif mode == "silent":
            self._send_json(200, {"ok": True, "tool": self.tool.name})
        elif mode == "header":
            self._send_json(200, {"ok": True, "tool": self.tool.name},
                            extra={CHALLENGE_HEADER: challenge})
        elif mode == "body":
            self._send_text(200, challenge)
        else:
            self._send_json(200, {"challenge": challenge, "ok": True, "tool": self.tool.name,
                                  "verbs": payload.get("verbs", [])})

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        self.tool.requests.append({"path": path, "body": b""})
        if path != HEALTH_PATH:
            self._send_json(404, {"error": "нет такого пути"})
            return
        if self.tool.health_ok:
            self._send_json(200, {"ok": True, "tool": self.tool.name, "handshakes": len(self.tool.requests)})
        else:
            self._send_json(503, {"ok": False, "tool": self.tool.name})


class FakeTool:
    """Инструмент на loopback: `base` — его endpoint, `stop()` — «гасим и уходим»."""

    def __init__(self, name: str = "pdf-summarizer", verbs=None, echo_mode: str = "json",
                 health_ok: bool = True, host: str = "127.0.0.1", port: int = 0,
                 handshake_delay: float = 0.0):
        if echo_mode not in ECHO_MODES:
            raise ValueError("неизвестный режим эха: %s" % echo_mode)
        self.name = name
        self.verbs = list(verbs or ["pdf.summarize"])
        self.echo_mode = echo_mode
        self.health_ok = health_ok
        self.handshake_delay = handshake_delay
        self.host = host
        self.requests: list[dict] = []
        handler = type("BoundTool", (_ToolHandler,), {"tool": self})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.port = self.httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def base(self) -> str:
        return "http://%s:%d" % (self.host, self.port)

    def start(self) -> "FakeTool":
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self, join_timeout: float = 5.0) -> None:
        """Погасить инструмент по-настоящему: сервер закрывается, порт освобождается."""
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        try:
            self.httpd.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None

    def __enter__(self) -> "FakeTool":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def handshake_payloads(self) -> list[dict]:
        out = []
        for request in self.requests:
            if request["path"] != HANDSHAKE_PATH:
                continue
            try:
                out.append(json.loads(request["body"].decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                out.append({})
        return out


def main(argv=None) -> int:
    """Ручной запуск фальшивого инструмента: удобно пробовать CLI на живом endpoint'е."""
    import argparse

    parser = argparse.ArgumentParser(prog="python3 -m regd.tools",
                                     description="Фальшивый инструмент реестра (loopback)")
    parser.add_argument("--name", default="pdf-summarizer")
    parser.add_argument("--verb", action="append", default=None,
                        help="возможность; можно несколько раз (по умолчанию pdf.summarize)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--mode", default="json", choices=ECHO_MODES,
                        help="как инструмент возвращает challenge")
    parser.add_argument("--unhealthy", action="store_true", help="отвечать 503 на проверку живости")
    args = parser.parse_args(argv)

    tool = FakeTool(name=args.name, verbs=args.verb or ["pdf.summarize"], echo_mode=args.mode,
                    health_ok=not args.unhealthy, host=args.host, port=args.port)
    print("инструмент %s слушает %s (возможности: %s, эхо challenge: %s)"
          % (tool.name, tool.base, ", ".join(tool.verbs), tool.echo_mode), flush=True)
    print("  POST %s%s · GET %s%s"
          % (tool.base, HANDSHAKE_PATH, tool.base, HEALTH_PATH), flush=True)
    try:
        tool.httpd.serve_forever()
    except KeyboardInterrupt:
        print("остановлен")
    finally:
        tool.httpd.server_close()
    return 0


if __name__ == "__main__":          # pragma: no cover
    import sys
    sys.exit(main())
