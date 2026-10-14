"""Рукопожатие и проверка живости: реестр дёргает инструмент, а не верит на слово.

Урок UDDI (запись архива `uddi`): 50 000 записей, из которых живых — единицы,
потому что никто никого не проверял. Поэтому регистрация в regd возможна только
после challenge: реестр отправляет случайное слово на endpoint инструмента, и
инструмент обязан его вернуть. Не подтвердил — записи нет.

Продление аренды — то же рукопожатие: «продлил» означает «инструмент жив и это
действительно он», а не «владелец нажал кнопку».

Протокол со стороны инструмента (свой вклад он подтверждает, а не описывает):

  POST /regd/handshake   JSON {"regd": "handshake", "challenge": "...", "name": ...,
                               "verbs": [...], "lease_seconds": N}
                         → 200 и тот же challenge обратно: полем JSON "challenge",
                           заголовком X-Regd-Challenge или телом-эхо;
  GET  /regd/health      → любой 2xx означает «жив».

Ошибки наружу не летят: обе функции возвращают dict с полем ok и причиной.
"""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request

from .model import CHALLENGE_HEADER, HANDSHAKE_PATH, HEALTH_PATH

HANDSHAKE_TIMEOUT = 3.0
PROBE_TIMEOUT = 2.0
MAX_BODY = 64 * 1024


def join(endpoint: str, path: str) -> str:
    return endpoint.rstrip("/") + path


def _post(url: str, payload: dict, timeout: float):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", "Accept": "application/json"})
    return urllib.request.urlopen(request, timeout=timeout)


def _get(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    return urllib.request.urlopen(request, timeout=timeout)


def echoed_challenge(raw: str, header: str, challenge: str):
    """Три законных способа вернуть challenge. Возвращает (подтверждено, способ/причина)."""
    if (header or "").strip() == challenge:
        return True, "заголовок %s" % CHALLENGE_HEADER
    text = (raw or "").strip()
    if text == challenge:
        return True, "эхо телом"
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False, "ответ не JSON и не эхо challenge"
    if isinstance(data, dict) and str(data.get("challenge", "")).strip() == challenge:
        return True, "поле challenge в JSON"
    return False, "challenge не подтверждён"


def verify_handshake(endpoint: str, name: str, verbs, lease_seconds: int,
                     timeout: float = HANDSHAKE_TIMEOUT, challenge: str | None = None) -> dict:
    """Проверка намерения: инструмент обязан вернуть присланный challenge."""
    challenge = challenge or secrets.token_hex(16)
    payload = {"regd": "handshake", "challenge": challenge, "name": name,
               "verbs": list(verbs), "lease_seconds": int(lease_seconds)}
    result = {"ok": False, "reason": "", "way": "", "challenge": challenge, "status": 0}
    try:
        with _post(join(endpoint, HANDSHAKE_PATH), payload, timeout) as response:
            status = response.status
            header = response.headers.get(CHALLENGE_HEADER, "")
            raw = response.read(MAX_BODY).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        result["status"] = code
        result["reason"] = "endpoint ответил %s" % code
        return result
    except (urllib.error.URLError, OSError) as exc:
        result["reason"] = "endpoint недоступен: %s" % exc
        return result
    except ValueError as exc:                    # кривой URL, который прошёл по виду
        result["reason"] = "endpoint не разобран: %s" % exc
        return result
    result["status"] = status
    if status // 100 != 2:
        result["reason"] = "endpoint ответил %s" % status
        return result
    ok, way = echoed_challenge(raw, header, challenge)
    if ok:
        result["ok"] = True
        result["way"] = way
        result["reason"] = ""
    else:
        result["ok"] = False
        result["way"] = ""
        result["reason"] = way
    return result


def probe_alive(endpoint: str, timeout: float = PROBE_TIMEOUT) -> dict:
    """Живость — отдельный вопрос к инструменту: любой 2xx на /regd/health."""
    try:
        with _get(join(endpoint, HEALTH_PATH), timeout) as response:
            status = response.status
            response.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return {"ok": False, "status": code, "reason": "проверка живости ответила %s" % code}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "status": 0, "reason": "недоступен: %s" % exc}
    ok = status // 100 == 2
    return {"ok": ok, "status": status,
            "reason": "" if ok else "проверка живости ответила %s" % status}
