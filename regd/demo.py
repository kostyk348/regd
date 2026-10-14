"""`regd demo` — сценарий одной командой, который доказывает работу запуском.

Поднимаем на loopback три фальшивых инструмента (живой, умирающий и глухой),
регистрируем только подтвердивших рукопожатие, продлеваем одного, гасим второго
и печатаем выдачу до и после `gc`. Ничего не изображается: сервера настоящие,
порты настоящие, время настоящее.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from .model import DEFAULT_LEASE, human_delta, iso
from .registry import Registry
from .render import archive_lines, rows_lines
from .tools import FakeTool


def run_demo(out=print, workdir: str | None = None, dying_lease: int = 3,
             live_lease: int = DEFAULT_LEASE, sleep=time.sleep,
             probe_timeout: float = 1.0) -> dict:
    tmp = workdir or tempfile.mkdtemp(prefix="regd-demo-")
    os.makedirs(tmp, exist_ok=True)
    store_path = os.path.join(tmp, "regd.json")
    journal_path = os.path.join(tmp, "regd-journal.jsonl")
    registry = Registry(store_path, journal_path=journal_path)

    live = FakeTool(name="pdf-summarizer", verbs=["pdf.summarize", "pdf.extract"])
    dying = FakeTool(name="legacy-summarizer", verbs=["pdf.summarize"])
    deaf = FakeTool(name="deaf-summarizer", verbs=["pdf.summarize"], echo_mode="wrong")
    tools = [live, dying, deaf]

    summary = {"ok": False, "registered": [], "refused": "", "deaf_recorded": None,
               "before_gc": 0, "after_gc": 0, "lapsed_before_gc": 0, "renewed": False,
               "expired": [], "store_path": store_path, "journal_path": journal_path}
    try:
        for tool in tools:
            tool.start()

        out("=== regd demo: реестр агентных инструментов с доверием и сроком годности ===")
        out("рабочая папка: %s" % tmp)
        out("")
        out("1. поднимаем на loopback три фальшивых инструмента")
        out("   живой      %-18s %s  возможности: %s" % (live.name, live.base, ", ".join(live.verbs)))
        out("   умирающий  %-18s %s  возможности: %s  аренда %s"
            % (dying.name, dying.base, ", ".join(dying.verbs), human_delta(dying_lease)))
        out("   глухой     %-18s %s  возможности: %s  (challenge не возвращает)"
            % (deaf.name, deaf.base, ", ".join(deaf.verbs)))
        out("")
        out("2. регистрация только после рукопожатия: реестр дёргает endpoint с challenge")
        for tool, lease in ((live, live_lease), (dying, dying_lease)):
            verdict = registry.register(tool.name, tool.base, tool.verbs,
                                        owner="demo", lease_seconds=lease,
                                        timeout=probe_timeout)
            if verdict["ok"]:
                out("   %-18s подтвердил challenge (%s) → id=%s, аренда %s"
                    % (tool.name, verdict["handshake"]["way"], verdict["id"], human_delta(lease)))
                summary["registered"].append(verdict["id"])
            else:
                out("   %-18s НЕ подтвердил: %s" % (tool.name, verdict["reason"]))
        verdict = registry.register(deaf.name, deaf.base, deaf.verbs,
                                    owner="demo", lease_seconds=60, timeout=probe_timeout)
        summary["refused"] = verdict["reason"]
        summary["deaf_recorded"] = bool(verdict.get("recorded"))
        out("   %-18s НЕ подтвердил: %s" % (deaf.name, verdict["reason"]))
        out("   → записи нет: в реестре %d записи, а не %d"
            % (len(registry.store.tools), len(registry.store.tools) + 1))
        out("")
        out("3. исходы вызовов от вызывающих: из них считается репутация числом")
        for ok in (True, True, False, True):
            registry.report(live.name, ok, "демо-вызов")
        for ok in (True, False, False):
            registry.report(dying.name, ok, "демо-вызов")
        out("   %s: 3 успеха / 1 провал;   %s: 1 успех / 2 провала"
            % (live.name, dying.name))
        out("")
        out("4. find --verb pdf.summarize — исполнитель выбирается по возможности")
        found = registry.find("pdf.summarize", probe=True, timeout=probe_timeout)
        for line in rows_lines(found["matches"], indent="   "):
            out(line)
        out("")
        out("5. продление аренды живого: аренду подтверждают рукопожатием, а не кнопкой")
        renewed = registry.renew(live.name, live_lease, timeout=probe_timeout)
        summary["renewed"] = bool(renewed["ok"])
        if renewed["ok"]:
            row = renewed["tool"]
            out("   аренда продлена до %s (%s), рукопожатие: %s"
                % (iso(row["expires_at"]), human_delta(row["expires_in"]),
                   renewed["handshake"]["way"]))
        else:
            out("   аренда не продлена: %s" % renewed["reason"])
        out("")
        out("6. гасим умирающий инструмент: сервер закрыт, отвечать больше некому")
        dying.stop()
        out("   %s остановлен" % dying.base)
        out("")
        out("7. ждём истечения его аренды (%s)…" % human_delta(dying_lease))
        sleep(dying_lease + 0.5)
        out("")
        out("8. выдача ДО `gc`: протухшая запись на месте и помечена, а не спрятана")
        before = registry.find("pdf.summarize", probe=True, timeout=probe_timeout)
        summary["before_gc"] = before["count"]
        summary["lapsed_before_gc"] = sum(1 for row in before["matches"] if row["lease_expired"])
        for line in rows_lines(before["matches"], indent="   "):
            out(line)
        out("")
        out("9. gc: протухшая запись уходит из выдачи в архив — с причиной, датой и историей")
        collected = registry.gc()
        summary["expired"] = [entry["name"] for entry in collected["expired"]]
        for entry in collected["expired"]:
            out("   перенесено в expired: %s" % entry["name"])
            for line in archive_lines([entry], indent="     "):
                out(line)
        out("   записей в архиве expired: %d" % collected["archive_size"])
        out("")
        out("10. выдача ПОСЛЕ `gc`: остался только подтверждённый инструмент")
        after = registry.find("pdf.summarize", probe=True, timeout=probe_timeout)
        summary["after_gc"] = after["count"]
        for line in rows_lines(after["matches"], indent="   "):
            out(line)
        again = registry.gc()
        out("   повторный `gc`: перенесено %d — сборка идемпотентна" % again["moved"])
        out("")
        out("состояние реестра: %s" % json.dumps(registry.health(), ensure_ascii=False))
        out("журнал (JSONL): %s" % journal_path)
        summary["ok"] = (summary["after_gc"] < summary["before_gc"]
                         and summary["lapsed_before_gc"] == 1
                         and summary["deaf_recorded"] is False
                         and summary["renewed"])
        out("итог: до gc %d записи (протухших %d) → после gc %d; глухой в реестр не попал; "
            "аренда живого продлена → сценарий %s"
            % (summary["before_gc"], summary["lapsed_before_gc"], summary["after_gc"],
               "сходится" if summary["ok"] else "НЕ сходится"))
    finally:
        for tool in tools:
            tool.stop()
    return summary
