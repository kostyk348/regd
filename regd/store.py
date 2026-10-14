"""Хранилище реестра: один JSON-файл с атомарной записью и журнал в JSONL.

Здесь живут аренда, репутация и архив протухших записей. Сетевые проверки
(рукопожатие, живость) — в `handshake.py`, решения — в `registry.py`.

Главное правило хранилища: **оно не удаляет записи**. Протухшую запись выносит
из оборота `gc`, но не стирает: она переезжает в `expired` вместе с причиной,
датой и историей исходов. Так урок UDDI («каталог — это обязательство») остаётся
верным и для мусора: из реестра запись уходит, из истории — нет.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from .model import (DEFAULT_LEASE, clamp_lease, human_delta, iso, last_signal,
                    normalize_verb, refresh_flags, reputation, sort_key, tool_id,
                    verb_matches, view)


class Store:
    def __init__(self, path: str, clock=time.time, journal_path: str | None = None):
        self.path = path
        self.clock = clock
        self.journal_path = journal_path
        self.tools: dict[str, dict] = {}
        self.expired: list[dict] = []
        self.journal: list[dict] = []
        self.load()

    # --- персистентность ---------------------------------------------------------
    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.tools = data.get("tools") or {}
        self.expired = data.get("expired") or []

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        payload = {"saved": self.clock(), "version": 1,
                   "tools": self.tools, "expired": self.expired}
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".regd-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def log(self, action: str, tid: str, ok: bool, note: str) -> dict:
        record = {"at": self.clock(), "action": action, "id": tid, "ok": bool(ok), "note": note}
        self.journal.append(record)
        if self.journal_path:
            try:
                with open(self.journal_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return record

    # --- записи ------------------------------------------------------------------
    def get(self, tid: str) -> dict | None:
        return self.tools.get(tid)

    def resolve(self, ref: str) -> dict | None:
        """Ссылка на запись: по id, либо по имени, если имя однозначно."""
        if not ref:
            return None
        if ref in self.tools:
            return self.tools[ref]
        named = [e for e in self.tools.values() if e.get("name") == ref]
        return named[0] if len(named) == 1 else None

    def archived(self, ref: str) -> dict | None:
        for entry in self.expired:
            if entry.get("id") == ref or entry.get("name") == ref:
                return entry
        return None

    def register(self, name: str, endpoint: str, verbs, owner: str = "",
                 lease_seconds: int | None = DEFAULT_LEASE, handshake_way: str = "") -> dict:
        """Создать запись (рукопожатие уже пройдено) либо обновить существующую."""
        now = self.clock()
        tid = tool_id(name, endpoint)
        lease = clamp_lease(DEFAULT_LEASE if lease_seconds is None else lease_seconds)
        entry = self.tools.get(tid)
        if entry is None:
            entry = {"id": tid, "name": name.strip(), "endpoint": endpoint.rstrip("/"),
                     "created": now, "ok": 0, "fail": 0, "probe_failures": 0, "alive": None,
                     "last_probe": None, "last_result": None, "probe_error": "",
                     "handshake_error": "", "last_error": "", "reports": []}
            self.tools[tid] = entry
        entry.update({
            "verbs": [normalize_verb(v) for v in verbs],
            "owner": owner or "",
            "lease_seconds": lease,
            "expires_at": now + lease,
            "renewed": now,
            "handshake_error": "",
            "last_handshake": {"at": now, "way": handshake_way},
        })
        entry["alive"] = True
        entry["last_probe"] = now
        entry["probe_failures"] = 0
        entry["probe_error"] = ""
        refresh_flags(entry)
        self.save()
        return entry

    def note_report(self, ref: str, ok: bool, note: str = "") -> dict | None:
        """Исход вызова от вызывающего: из этих чисел считается репутация."""
        entry = self.resolve(ref)
        if entry is None:
            return None
        now = self.clock()
        if ok:
            entry["ok"] = int(entry.get("ok", 0)) + 1
            entry["last_error"] = ""
        else:
            entry["fail"] = int(entry.get("fail", 0)) + 1
            entry["last_error"] = note or "провал вызова"
        entry["last_result"] = now
        reports = list(entry.get("reports") or [])
        reports.append({"at": now, "ok": bool(ok), "note": note})
        entry["reports"] = reports[-20:]
        refresh_flags(entry)
        self.save()
        return entry

    def note_probe(self, ref: str, alive: bool, reason: str = "") -> dict | None:
        entry = self.resolve(ref)
        if entry is None:
            return None
        now = self.clock()
        entry["alive"] = bool(alive)
        entry["last_probe"] = now
        if alive:
            entry["probe_failures"] = 0
            entry["probe_error"] = ""
        else:
            entry["probe_failures"] = int(entry.get("probe_failures", 0)) + 1
            entry["probe_error"] = reason or "не ответил на проверку живости"
        refresh_flags(entry)
        self.save()
        return entry

    def renew(self, ref: str, lease_seconds: int | None = None, handshake_way: str = "") -> dict | None:
        entry = self.resolve(ref)
        if entry is None:
            return None
        now = self.clock()
        base = entry.get("lease_seconds")
        lease = clamp_lease(base if lease_seconds is None else lease_seconds)
        entry.update({"lease_seconds": lease, "expires_at": now + lease, "renewed": now,
                      "handshake_error": "", "probe_failures": 0, "probe_error": "",
                      "alive": True, "last_probe": now,
                      "last_handshake": {"at": now, "way": handshake_way}})
        refresh_flags(entry)
        self.save()
        return entry

    def fail_handshake(self, ref: str, reason: str) -> dict | None:
        """Рукопожатие не подтвердилось: запись остаётся, но честно помечена."""
        entry = self.resolve(ref)
        if entry is None:
            return None
        now = self.clock()
        entry["handshake_error"] = reason
        entry["alive"] = False
        entry["last_probe"] = now
        entry["probe_failures"] = int(entry.get("probe_failures", 0)) + 1
        entry["probe_error"] = reason
        refresh_flags(entry)
        self.save()
        return entry

    # --- сборка мусора ------------------------------------------------------------
    def gc(self) -> list[dict]:
        """Протухшие записи уходят из оборота в архив — с причиной, датой и историей."""
        now = self.clock()
        moved = []
        for tid in sorted(self.tools):
            entry = self.tools[tid]
            expires_at = float(entry.get("expires_at", 0) or 0)
            if expires_at > now:
                continue
            self.tools.pop(tid)
            archived = dict(entry)
            archived["history"] = {
                "ok": int(entry.get("ok", 0) or 0),
                "fail": int(entry.get("fail", 0) or 0),
                "reputation_at_expiry": reputation(entry, now),
                "last_signal": last_signal(entry),
                "last_error": entry.get("last_error", ""),
                "probe_error": entry.get("probe_error", ""),
            }
            archived["expired_at"] = now
            archived["expired_reason"] = (
                "аренда истекла в %s: продления не было %s, последний сигнал %s"
                % (iso(expires_at), human_delta(now - expires_at),
                   iso(last_signal(entry)) or "нет"))
            self.expired.append(archived)
            self.log("gc", tid, True, archived["expired_reason"])
            moved.append(archived)
        if moved:
            self.save()
        return moved

    # --- чтение -------------------------------------------------------------------
    def rows(self, verb: str | None = None, live_only: bool = False) -> list[dict]:
        now = self.clock()
        out = [view(entry, now) for entry in self.tools.values()]
        if verb:
            out = [row for row in out if verb_matches(verb, row["verbs"])]
        if live_only:
            out = [row for row in out if not row["lease_expired"]]
        out.sort(key=sort_key)
        return out

    def lapsed_rows(self) -> list[dict]:
        now = self.clock()
        return [view(entry, now) for entry in self.tools.values()
                if float(entry.get("expires_at", 0) or 0) <= now]

    def expired_rows(self) -> list[dict]:
        return [dict(entry) for entry in self.expired]
