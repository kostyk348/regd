"""Текстовый вывод для человека (CLI и `demo`).

Весь машинный обмен идёт JSON'ом — здесь только то, что человек читает глазами
в терминале. Никакая логика не разбирает эти строки обратно.
"""
from __future__ import annotations

from .model import alive_phrase, human_delta, iso, lease_phrase


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def marks_phrase(row: dict) -> str:
    marks = []
    if row["degraded"]:
        marks.append("degraded: %s" % "; ".join(row["degraded_reasons"]))
    if row["lease_expired"]:
        marks.append("аренда истекла %s назад — ждёт `regd gc`"
                     % human_delta(-row["expires_in"]))
    if row["alive"] is False:
        marks.append("нет ответа на проверку живости: %s"
                     % (row.get("probe_error") or "проверка не прошла"))
    return "; ".join(marks)


def tool_lines(row: dict, indent: str = "  ", now: float | None = None) -> list[str]:
    lines = ["%s%s  [%s]" % (indent, row["name"], row["id"])]
    lines.append("%s  endpoint %s%s" % (indent, row["endpoint"],
                                        "   владелец: %s" % row["owner"] if row["owner"] else ""))
    lines.append("%s  возможности: %s" % (indent, ", ".join(row["verbs"]) or "—"))
    lines.append("%s  аренда: %s (до %s)   живость: %s"
                 % (indent, lease_phrase(row["expires_in"]), iso(row["expires_at"]),
                    alive_phrase(row["alive"], row["last_probe"], now if now is not None
                                 else float(row["last_probe"] or 0))))
    ratio = "—" if row["success_ratio"] is None else "%.2f" % row["success_ratio"]
    lines.append("%s  исходы: %d %s / %d %s, доля %s, свежесть %.2f → репутация %.3f"
                 % (indent, row["ok"], _plural(row["ok"], "успех", "успеха", "успехов"),
                    row["fail"], _plural(row["fail"], "провал", "провала", "провалов"),
                    ratio, row["freshness"], row["reputation"]))
    marks = marks_phrase(row)
    if marks:
        lines.append("%s  метки: %s" % (indent, marks))
    return lines


def rows_lines(rows, indent: str = "  ", empty: str = "  (ничего не найдено)") -> list[str]:
    if not rows:
        return [empty]
    out = []
    for index, row in enumerate(rows):
        if index:
            out.append("")
        out.extend(tool_lines(row, indent=indent))
    return out


def archived_lines(entry: dict, indent: str = "  ") -> list[str]:
    lines = ["%s%s  [%s]  в архиве с %s" % (indent, entry["name"], entry["id"],
                                            entry.get("expired_at_iso") or iso(entry.get("expired_at")))]

    lines.append("%s  endpoint %s   возможности: %s"
                 % (indent, entry["endpoint"], ", ".join(entry.get("verbs") or []) or "—"))
    lines.append("%s  причина: %s" % (indent, entry.get("expired_reason", "")))
    history = entry.get("history") or {}
    if history:
        ok_count = int(history.get("ok", 0) or 0)
        fail_count = int(history.get("fail", 0) or 0)
        lines.append("%s  история: %d %s / %d %s, репутация на момент истечения %s"
                     % (indent, ok_count, _plural(ok_count, "успех", "успеха", "успехов"),
                        fail_count, _plural(fail_count, "провал", "провала", "провалов"),
                        history.get("reputation_at_expiry", "—")))
    return lines


def archive_lines(entries, indent: str = "  ") -> list[str]:
    if not entries:
        return ["%s(архив пуст)" % indent]
    out = []
    for index, entry in enumerate(entries):
        if index:
            out.append("")
        out.extend(archived_lines(entry, indent=indent))
    return out
