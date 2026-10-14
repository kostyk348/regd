"""Модель записи реестра: идентификатор, аренда, репутация.

Три правила — из находок музея, и здесь они код, а не лозунг:

  * `uddi` (архив музея): публичный каталог без модерации, без денег за хостинг и без
    срока годности записи превратился в свалку — 50 000 записей, живых единицы,
    12.01.2006 публикацию выключили. Поэтому запись в regd — аренда: она живёт,
    пока владелец её продлевает.
  * `whoispp`: структурный доступ к реестру проиграл текстовому WHOIS и вернулся
    только как RDAP в 2015 году. Поэтому у regd два машинных входа (JSON-out и
    HTTP-API) и ни одной функции, которая разбирает человеческий вывод.
  * `webintents`: исполнитель выбирается в рантайме по возможности. Поэтому
    `find --verb` ищет по умению, а репутация отдаётся числом, а не звёздочками.
"""
from __future__ import annotations

import datetime
import fnmatch
import hashlib
import math
import re

# --- аренда -------------------------------------------------------------------
DEFAULT_LEASE = 3600
MIN_LEASE = 1
MAX_LEASE = 30 * 24 * 3600

# --- возможность (verb) -------------------------------------------------------
VERB_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

# --- репутация ----------------------------------------------------------------
NEUTRAL_RATIO = 0.5          # исходов ещё нет: репутация нейтральная, а не нулевая
FRESHNESS_HALF_LIFE = 12 * 3600.0
DEGRADE_AFTER_PROBES = 3     # три неотвеченные проверки живости подряд — degraded
DEGRADE_MIN_ATTEMPTS = 5     # меньше пяти исходов — по доле успехов судить рано
DEGRADE_MAX_RATIO = 0.2      # доля успехов ниже этого порога при достатке исходов

# --- протокол инструмента -----------------------------------------------------
HANDSHAKE_PATH = "/regd/handshake"
HEALTH_PATH = "/regd/health"
CHALLENGE_HEADER = "X-Regd-Challenge"


def normalize_verb(verb: str) -> str:
    return (verb or "").strip().lower()


def valid_verb(verb: str) -> bool:
    return bool(VERB_RE.match(normalize_verb(verb)))


def tool_id(name: str, endpoint: str) -> str:
    """Одно имя на одном endpoint — одна запись (устойчивый id, как у подписки в hubd)."""
    key = "%s\n%s" % ((name or "").strip(), (endpoint or "").strip().rstrip("/"))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def clamp_lease(seconds) -> int:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        value = DEFAULT_LEASE
    return max(MIN_LEASE, min(value, MAX_LEASE))


def success_ratio(ok: int, fail: int):
    attempts = int(ok) + int(fail)
    return None if attempts == 0 else int(ok) / attempts


def last_signal(entry: dict) -> float:
    """Последний сигнал от инструмента: исход вызова, либо продление аренды, либо регистрация."""
    for key in ("last_result", "renewed", "created"):
        value = entry.get(key)
        if value:
            return float(value)
    return 0.0


def freshness(value: float, now: float, half_life: float = FRESHNESS_HALF_LIFE) -> float:
    """Свежесть: 1.0 — сигнал только что, 0.5 — полпериода назад, дальше к нулю."""
    if not value:
        return 0.0
    age = max(0.0, float(now) - float(value))
    return math.exp(-age * math.log(2.0) / max(1.0, half_life))


def reputation(entry: dict, now: float) -> float:
    """Репутация = доля успехов × свежесть. Число, и видно, из чего оно сложилось."""
    ratio = success_ratio(entry.get("ok", 0), entry.get("fail", 0))
    return round((NEUTRAL_RATIO if ratio is None else ratio) * freshness(last_signal(entry), now), 3)


def refresh_flags(entry: dict) -> dict:
    """Пересчитать метку degraded. Метки лечатся: причина ушла — метка снята."""
    reasons = []
    failures = int(entry.get("probe_failures", 0) or 0)
    if failures >= DEGRADE_AFTER_PROBES:
        reasons.append("не ответил на %d проверок живости подряд" % failures)
    if entry.get("handshake_error"):
        reasons.append("рукопожатие не подтверждено: %s" % entry["handshake_error"])
    attempts = int(entry.get("ok", 0) or 0) + int(entry.get("fail", 0) or 0)
    ratio = success_ratio(entry.get("ok", 0), entry.get("fail", 0))
    if attempts >= DEGRADE_MIN_ATTEMPTS and ratio is not None and ratio < DEGRADE_MAX_RATIO:
        reasons.append("доля успехов %.2f при %d исходах ниже порога %.2f"
                       % (ratio, attempts, DEGRADE_MAX_RATIO))
    entry["degraded"] = bool(reasons)
    entry["degraded_reasons"] = reasons
    return entry


def verb_matches(query: str, verbs) -> bool:
    """Структурный поиск: точное совпадение возможности или шаблон (`pdf.*`)."""
    q = normalize_verb(query)
    if not q:
        return True
    if "*" in q or "?" in q:
        return any(fnmatch.fnmatchcase(normalize_verb(v), q) for v in verbs)
    return q in {normalize_verb(v) for v in verbs}


def iso(ts) -> str:
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


def human_delta(seconds) -> str:
    seconds = abs(int(seconds))
    if seconds < 60:
        return "%dс" % seconds
    if seconds < 3600:
        return "%dм %dс" % (seconds // 60, seconds % 60)
    if seconds < 86400:
        return "%dч %dм" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dд %dч" % (seconds // 86400, (seconds % 86400) // 3600)


def lease_phrase(expires_in: int) -> str:
    if expires_in >= 0:
        return "в запасе %s" % human_delta(expires_in)
    return "истекла %s назад" % human_delta(-expires_in)


def alive_phrase(alive, last_probe, now) -> str:
    if alive is None:
        return "не проверялась"
    if alive:
        return "да (проверено %s назад)" % human_delta(now - float(last_probe or now))
    return "нет (проверено %s назад)" % human_delta(now - float(last_probe or now))


def view(entry: dict, now: float) -> dict:
    """Запись в том виде, в каком её отдают CLI и HTTP: числами, а не прозой."""
    attempts = int(entry.get("ok", 0) or 0) + int(entry.get("fail", 0) or 0)
    ratio = success_ratio(entry.get("ok", 0), entry.get("fail", 0))
    expires_at = float(entry.get("expires_at", 0) or 0)
    return {
        "id": entry["id"],
        "name": entry["name"],
        "endpoint": entry["endpoint"],
        "owner": entry.get("owner", ""),
        "verbs": list(entry.get("verbs", [])),
        "lease_seconds": int(entry.get("lease_seconds", DEFAULT_LEASE)),
        "created": entry.get("created"),
        "renewed": entry.get("renewed"),
        "expires_at": expires_at,
        "expires_in": int(expires_at - now),
        "lease_expired": expires_at <= now,
        "ok": int(entry.get("ok", 0) or 0),
        "fail": int(entry.get("fail", 0) or 0),
        "attempts": attempts,
        "success_ratio": None if ratio is None else round(ratio, 3),
        "freshness": round(freshness(last_signal(entry), now), 3),
        "reputation": reputation(entry, now),
        "last_result": entry.get("last_result"),
        "alive": entry.get("alive"),
        "last_probe": entry.get("last_probe"),
        "probe_failures": int(entry.get("probe_failures", 0) or 0),
        "probe_error": entry.get("probe_error", ""),
        "degraded": bool(entry.get("degraded")),
        "degraded_reasons": list(entry.get("degraded_reasons", [])),
        "last_error": entry.get("last_error", ""),
        "handshake_error": entry.get("handshake_error", ""),
    }


def sort_key(row: dict):
    """Живые раньше протухших, не-degraded раньше degraded, отвечающие раньше молчащих."""
    alive_rank = {True: 0, None: 1, False: 2}[row["alive"]]
    return (row["lease_expired"], row["degraded"], alive_rank, -row["reputation"], row["name"])


def archived_view(entry: dict) -> dict:
    """Запись из архива `expired`: с причиной, датой и историей — её не удаляли молча."""
    return {
        "id": entry["id"],
        "name": entry["name"],
        "endpoint": entry["endpoint"],
        "owner": entry.get("owner", ""),
        "verbs": list(entry.get("verbs", [])),
        "created": entry.get("created"),
        "renewed": entry.get("renewed"),
        "lease_seconds": int(entry.get("lease_seconds", DEFAULT_LEASE)),
        "expired_at": entry.get("expired_at"),
        "expired_at_iso": iso(entry.get("expired_at")),
        "expired_reason": entry.get("expired_reason", ""),
        "history": dict(entry.get("history") or {}),
        "reports": list(entry.get("reports") or []),
    }
