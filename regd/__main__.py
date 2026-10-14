"""CLI реестра: register, find, list, report, renew, gc, serve, demo.

Коды возврата (для скриптов и тестов):
  0 — получилось;
  2 — ошибка ввода (нет имени, некорректная возможность, не разобрано тело);
  3 — отказ по контракту: рукопожатие не подтверждено, аренда не продлена, записи нет.

С `--json` любая команда печатает один JSON-объект — машинам не нужно разбирать
человеческий текст (урок `whoispp`: доступ к реестру — структура, а не парсинг).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .model import DEFAULT_LEASE, human_delta, iso
from .registry import Registry, build_server
from .render import archive_lines, rows_lines


def default_store() -> str:
    return os.environ.get("REGD_STORE") or "regd.json"


def default_journal(store_path: str) -> str:
    if os.environ.get("REGD_JOURNAL"):
        return os.environ["REGD_JOURNAL"]
    directory = os.path.dirname(os.path.abspath(store_path))
    base = os.path.basename(store_path)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return os.path.join(directory, stem + "-journal.jsonl")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="regd",
        description="Реестр агентных инструментов: рукопожатие, аренда, репутация числом, архив протухших записей")
    parser.add_argument("--store", default=default_store(),
                        help="JSON-хранилище реестра (по умолчанию %(default)s, env REGD_STORE)")
    parser.add_argument("--journal", default=None,
                        help="журнал событий JSONL (по умолчанию рядом с хранилищем, env REGD_JOURNAL)")
    parser.add_argument("--json", action="store_true", help="машинный вывод: один JSON-объект")
    parser.add_argument("--version", action="version", version="regd %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", help="зарегистрировать инструмент (только после рукопожатия)")
    p.add_argument("--name", required=True, help="имя инструмента, например pdf-summarizer")
    p.add_argument("--endpoint", required=True, help="базовый URL, например http://127.0.0.1:8080")
    p.add_argument("--verb", action="append", default=[], required=True,
                   help="возможность, например pdf.summarize (можно несколько раз)")
    p.add_argument("--owner", default="", help="владелец записи")
    p.add_argument("--lease", type=int, default=DEFAULT_LEASE, help="срок аренды в секундах")
    p.add_argument("--timeout", type=float, default=None, help="таймаут рукопожатия, секунды")

    p = sub.add_parser("find", help="найти исполнителя по возможности")
    p.add_argument("--verb", default=None, help="возможность или шаблон, например pdf.*")
    p.add_argument("--live", action="store_true", help="только записи с действующей арендой")
    p.add_argument("--no-probe", action="store_true", help="не дёргать проверку живости")
    p.add_argument("--timeout", type=float, default=None)

    p = sub.add_parser("list", help="состояние: аренда, живость, успехи/провалы, метки")
    p.add_argument("--no-probe", action="store_true", help="не дёргать проверку живости")
    p.add_argument("--timeout", type=float, default=None)

    p = sub.add_parser("report", help="оставить исход вызова: --ok или --fail")
    p.add_argument("--id", required=True, help="id записи или её имя")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--ok", action="store_true", help="вызов удался")
    group.add_argument("--fail", action="store_true", help="вызов провалился")
    p.add_argument("--note", default="", help="короткая причина провала")

    p = sub.add_parser("renew", help="продлить аренду (подтверждается рукопожатием)")
    p.add_argument("--id", required=True)
    p.add_argument("--lease", type=int, default=None, help="новый срок аренды в секундах")
    p.add_argument("--timeout", type=float, default=None)

    sub.add_parser("gc", help="собрать мусор: протухшее — в архив expired с причиной и датой")

    p = sub.add_parser("serve", help="HTTP-фасад: GET /tools, /find?verb=…, /healthz, POST /report")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)

    p = sub.add_parser("demo", help="одна команда: два инструмента, продление, смерть, gc (до/после)")
    p.add_argument("--workdir", default=None, help="куда положить хранилище демо (по умолчанию временная папка)")
    p.add_argument("--dying-lease", type=int, default=3, help="аренда умирающего инструмента, секунды")

    # `--json` работает и до команды, и после неё: `regd --json find` == `regd find --json`.
    for subparser in sub.choices.values():
        subparser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                               help="машинный вывод: один JSON-объект (то же, что --json до команды)")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "demo":
        from .demo import run_demo
        summary = run_demo(workdir=args.workdir, dying_lease=args.dying_lease)
        return 0 if summary.get("ok") else 1

    registry = Registry(args.store, journal_path=args.journal or default_journal(args.store))

    if args.command == "register":
        verdict = registry.register(args.name, args.endpoint, args.verb, args.owner,
                                   args.lease, args.timeout)
    elif args.command == "find":
        verdict = registry.find(args.verb, probe=not args.no_probe, live_only=args.live,
                                timeout=args.timeout)
    elif args.command == "list":
        verdict = registry.state(probe=not args.no_probe, timeout=args.timeout)
    elif args.command == "report":
        verdict = registry.report(args.id, bool(args.ok), args.note)
    elif args.command == "renew":
        verdict = registry.renew(args.id, args.lease, args.timeout)
    elif args.command == "gc":
        verdict = registry.gc()
    elif args.command == "serve":
        return _serve(registry, args)
    else:                                     # pragma: no cover - argparse не пустит
        parser.error("неизвестная команда: %s" % args.command)

    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=1))
        return int(verdict.get("exit", 0))
    _print_text(args.command, verdict)
    return int(verdict.get("exit", 0))


def _print_text(command: str, verdict: dict) -> None:
    if command == "register":
        if verdict["ok"]:
            row = verdict["tool"]
            print("зарегистрирован: %s  [%s]" % (row["name"], row["id"]))
            print("  endpoint %s   возможности: %s" % (row["endpoint"], ", ".join(row["verbs"])))
            print("  рукопожатие подтверждено (%s); аренда %s, до %s"
                  % (verdict["handshake"]["way"], human_delta(row["expires_in"]),
                     iso(row["expires_at"])))
        else:
            print("ОТКАЗ: %s" % verdict["reason"], file=sys.stderr)
            if verdict.get("recorded"):
                print("  запись осталась в реестре и помечена degraded", file=sys.stderr)
            else:
                print("  записи нет: реестр не принимает инструмент, который не подтвердил challenge",
                      file=sys.stderr)
        return

    if command == "find":
        head = "по возможности %s" % ("«%s»" % verdict["verb"] if verdict["verb"] else "(все)")
        print("%s: %d" % (head, verdict["count"]))
        for line in rows_lines(verdict["matches"], indent="  "):
            print(line)
        return

    if command == "list":
        rows = verdict["tools"]
        print("записей: %d (живых %d, протухших %d, degraded %d)"
              % (verdict["count"], verdict["live"], verdict["lapsed"], verdict["degraded"]))
        for line in rows_lines(rows, indent="  "):
            print(line)
        print()
        print("архив expired: %d" % verdict["expired_count"])
        for line in archive_lines(verdict["expired"], indent="  "):
            print(line)
        return

    if command == "report":
        row = verdict["tool"]
        print("исход принят: %s [%s] — %s" % (row["name"], row["id"],
                                              "успех" if verdict["accepted"] else "провал"))
        print("  исходы: %d успехов / %d провалов, доля %s, репутация %.3f"
              % (row["ok"], row["fail"],
                 "—" if row["success_ratio"] is None else "%.2f" % row["success_ratio"],
                 row["reputation"]))
        if row["degraded"]:
            print("  метки: degraded — %s" % "; ".join(row["degraded_reasons"]))
        return

    if command == "renew":
        if verdict["ok"]:
            row = verdict["tool"]
            print("аренда продлена: %s [%s] до %s (осталось %s), рукопожатие: %s"
                  % (row["name"], row["id"], iso(row["expires_at"]),
                     human_delta(row["expires_in"]), verdict["handshake"]["way"]))
        else:
            print("ОТКАЗ: %s" % verdict["reason"], file=sys.stderr)
        return

    if command == "gc":
        print("перенесено в expired: %d; записей в архиве: %d"
              % (verdict["moved"], verdict["archive_size"]))
        for line in archive_lines(verdict["expired"], indent="  "):
            print(line)
        return


def _serve(registry: Registry, args) -> int:
    server = build_server(registry, args.host, args.port)
    host, port = server.server_address[:2]
    print("regd слушает http://%s:%d" % (host, port), flush=True)
    print("  GET  /healthz · GET /tools · GET /find?verb=…[&live=1&probe=1] · POST /report {id, ok, note}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("остановлен")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":                 # pragma: no cover
    sys.exit(main())
