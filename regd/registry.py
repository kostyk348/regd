"""Реестр: одна реализация логики, два входа — CLI и HTTP-фасад.

`Registry` держит правила («регистрация только после рукопожатия», «продление
подтверждается повторно», «gc вместо тихого удаления»), а `serve` вызывает те же
методы по HTTP. Никакой разбор человеческого текста: обе стороны говорят JSON —
это урок `whoispp` (структурный доступ к реестру) и `webintents` (выбор исполнителя
в рантайме по возможности, а не по имени сервиса).
"""
from __future__ import annotations

import json
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import handshake as hs
from .model import (DEFAULT_LEASE, archived_view, clamp_lease, normalize_verb,
                    tool_id, valid_verb, view)
from .store import Store

MAX_BODY = 64 * 1024

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3


class Registry:
    def __init__(self, store_path: str, clock=time.time, journal_path: str | None = None,
                 timeout: float = hs.HANDSHAKE_TIMEOUT):
        self.store = Store(store_path, clock=clock, journal_path=journal_path)
        self.clock = clock
        self.timeout = timeout

    # --- 1. регистрация -----------------------------------------------------------
    def register(self, name: str, endpoint: str, verbs, owner: str = "",
                 lease_seconds: int = DEFAULT_LEASE, timeout: float | None = None) -> dict:
        name = (name or "").strip()
        endpoint = (endpoint or "").strip().rstrip("/")
        verbs = [normalize_verb(v) for v in (verbs or []) if normalize_verb(v)]
        if not name:
            return {"ok": False, "exit": EXIT_USAGE, "reason": "нужно имя инструмента (--name)"}
        if not endpoint.startswith(("http://", "https://")):
            return {"ok": False, "exit": EXIT_USAGE,
                    "reason": "нужен endpoint вида http://host:port (--endpoint)"}
        if not verbs:
            return {"ok": False, "exit": EXIT_USAGE,
                    "reason": "нужна хотя бы одна возможность (--verb)"}
        bad = [v for v in verbs if not valid_verb(v)]
        if bad:
            return {"ok": False, "exit": EXIT_USAGE,
                    "reason": "некорректные возможности: %s" % ", ".join(bad)}
        lease = clamp_lease(lease_seconds or DEFAULT_LEASE)
        tid = tool_id(name, endpoint)
        verdict = hs.verify_handshake(endpoint, name, verbs, lease, timeout or self.timeout)
        if not verdict["ok"]:
            existing = self.store.fail_handshake(tid, verdict["reason"])
            if existing is not None:
                note = "запись не обновлена, помечена degraded: %s" % verdict["reason"]
            else:
                note = "записи нет: %s" % verdict["reason"]
            self.store.log("register", tid, False, note)
            return {"ok": False, "exit": EXIT_REFUSED, "reason": verdict["reason"],
                    "id": tid, "handshake": verdict, "recorded": existing is not None}
        entry = self.store.register(name, endpoint, verbs, owner, lease, verdict["way"])
        self.store.log("register", entry["id"], True,
                       "рукопожатие подтверждено (%s), аренда %ss" % (verdict["way"], lease))
        return {"ok": True, "exit": EXIT_OK, "reason": "", "id": entry["id"],
                "handshake": verdict, "tool": view(entry, self.clock())}

    # --- 2. поиск по возможностям -------------------------------------------------
    def find(self, verb: str | None = None, probe: bool = True, live_only: bool = False,
             timeout: float | None = None) -> dict:
        if probe:
            self.probe_matching(verb, live_only)
        rows = self.store.rows(verb=verb, live_only=live_only)
        return {"ok": True, "exit": EXIT_OK, "verb": verb or "", "probe": bool(probe),
                "count": len(rows), "matches": rows}

    def probe_matching(self, verb: str | None = None, live_only: bool = False):
        for row in self.store.rows(verb=verb, live_only=live_only):
            self.probe(row["id"], timeout=None)

    def probe(self, ref: str, timeout: float | None = None) -> dict | None:
        entry = self.store.resolve(ref)
        if entry is None:
            return None
        verdict = hs.probe_alive(entry["endpoint"], timeout or hs.PROBE_TIMEOUT)
        self.store.note_probe(entry["id"], verdict["ok"], verdict["reason"])
        return verdict

    # --- 3. состояние -------------------------------------------------------------
    def state(self, probe: bool = True, timeout: float | None = None) -> dict:
        if probe:
            for tid in list(self.store.tools):
                self.probe(tid, timeout=timeout)
        rows = self.store.rows()
        lapsed = [row for row in rows if row["lease_expired"]]
        return {"ok": True, "exit": EXIT_OK, "now": int(self.clock()),
                "count": len(rows), "live": len(rows) - len(lapsed), "lapsed": len(lapsed),
                "degraded": sum(1 for row in rows if row["degraded"]),
                "tools": rows,
                "expired_count": len(self.store.expired),
                "expired": self.archive()}

    def archive(self) -> list[dict]:
        return [archived_view(entry) for entry in self.store.expired_rows()]

    # --- 4. исходы вызовов --------------------------------------------------------
    def report(self, ref: str, ok: bool, note: str = "") -> dict:
        entry = self.store.resolve(ref)
        if entry is None:
            archived = self.store.archived(ref)
            reason = ("запись в архиве expired: %s" % archived["expired_reason"]) if archived \
                else "записи с таким id или именем нет"
            self.store.log("report", ref, False, reason)
            return {"ok": False, "exit": EXIT_REFUSED, "reason": reason}
        entry = self.store.note_report(entry["id"], ok, note)
        self.store.log("report", entry["id"], ok, note or ("успех" if ok else "провал"))
        return {"ok": True, "exit": EXIT_OK, "accepted": bool(ok),
                "tool": view(entry, self.clock())}

    # --- 5. аренда и сборка мусора ------------------------------------------------
    def renew(self, ref: str, lease_seconds: int | None = None, timeout: float | None = None) -> dict:
        entry = self.store.resolve(ref)
        if entry is None:
            self.store.log("renew", ref, False, "записи нет")
            return {"ok": False, "exit": EXIT_REFUSED,
                    "reason": "записи нет: проверьте --id (архив expired не продлевается)"}
        base = entry.get("lease_seconds") or DEFAULT_LEASE
        lease = clamp_lease(base if lease_seconds is None else lease_seconds)
        verdict = hs.verify_handshake(entry["endpoint"], entry["name"], entry["verbs"],
                                      lease, timeout or self.timeout)
        if not verdict["ok"]:
            self.store.fail_handshake(entry["id"], verdict["reason"])
            self.store.log("renew", entry["id"], False, "аренда не продлена: %s" % verdict["reason"])
            return {"ok": False, "exit": EXIT_REFUSED,
                    "reason": "аренда не продлена: %s" % verdict["reason"],
                    "handshake": verdict, "id": entry["id"]}
        self.store.renew(entry["id"], lease, verdict["way"])
        self.store.log("renew", entry["id"], True,
                       "аренда %ss, рукопожатие подтверждено (%s)" % (lease, verdict["way"]))
        return {"ok": True, "exit": EXIT_OK, "handshake": verdict, "id": entry["id"],
                "tool": view(self.store.get(entry["id"]), self.clock())}

    def gc(self) -> dict:
        moved = self.store.gc()
        return {"ok": True, "exit": EXIT_OK, "moved": len(moved),
                "archive_size": len(self.store.expired),
                "expired": [archived_view(entry) for entry in moved]}

    # --- 6. фасад -----------------------------------------------------------------
    def health(self) -> dict:
        rows = self.store.rows()
        return {"ok": True, "tools": len(rows),
                "live": sum(1 for row in rows if not row["lease_expired"]),
                "lapsed": sum(1 for row in rows if row["lease_expired"]),
                "degraded": sum(1 for row in rows if row["degraded"]),
                "expired": len(self.store.expired), "now": int(self.clock())}


class _Handler(BaseHTTPRequestHandler):
    registry: "Registry" = None          # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):     # тишина: журнал ведёт реестр
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        length = min(int(self.headers.get("Content-Length", "0") or 0), MAX_BODY)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            return None, "тело не разобрано как JSON: %s" % exc
        if not isinstance(data, dict):
            return None, "тело должно быть JSON-объектом"
        return data, ""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/healthz":
            self._send(200, self.registry.health())
        elif parsed.path == "/tools":
            self._send(200, self.registry.state(probe=False))
        elif parsed.path == "/find":
            verb = (query.get("verb") or [""])[0].strip()
            if not verb:
                self._send(400, {"error": "нужен ?verb=… (структурный поиск, не текст)",
                                 "ok": False, "exit": EXIT_USAGE})
                return
            probe = (query.get("probe") or ["0"])[0].lower() not in ("", "0", "false", "no")
            live = (query.get("live") or ["0"])[0].lower() in ("1", "true", "yes")
            self._send(200, self.registry.find(verb, probe=probe, live_only=live))
        else:
            self._send(404, {"error": "нет такого пути", "ok": False})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/report":
            self._send(404, {"error": "нет такого пути", "ok": False})
            return
        data, error = self._json_body()
        if data is None:
            self._send(400, {"error": error, "ok": False, "exit": EXIT_USAGE})
            return
        tid = str(data.get("id") or "").strip()
        if not tid:
            self._send(400, {"error": "нужно поле id", "ok": False, "exit": EXIT_USAGE})
            return
        if "ok" not in data or not isinstance(data["ok"], bool):
            self._send(400, {"error": "нужно булево поле ok", "ok": False, "exit": EXIT_USAGE})
            return
        verdict = self.registry.report(tid, bool(data["ok"]), str(data.get("note") or ""))
        if not verdict["ok"]:
            self._send(404, verdict)
            return
        self._send(200, verdict)


def build_server(registry: Registry, host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {"registry": registry})
    return ThreadingHTTPServer((host, port), handler)


def serve(registry: Registry, host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    server = build_server(registry, host, port)
    return server
