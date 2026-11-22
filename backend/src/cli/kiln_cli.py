"""窑务台账命令行副本台。

用法示例：

    python -m src.cli.kiln_cli --db ./work/kiln.db manifest 1 0 A B
    python -m src.cli.kiln_cli receive 1 A "长石 60 g" --event evt-1
    python -m src.cli.kiln_cli pump
    python -m src.cli.kiln_cli status
    python -m src.cli.kiln_cli read A --at 1
    python -m src.cli.kiln_cli withdraw A "配方撤下待核准"

所有命令默认输出人读表格；加 --json 输出确定性 JSON，便于脚本对接。
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from src.services.kiln import Kiln, KilnError
from src.services.kiln.store import Store

DEFAULT_DB = "work/kiln-ledger.db"


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("（无记录）")
        return
    headers = list(rows[0].keys())
    rendered = [[("" if value is None else str(value)) for value in row.values()]
                for row in rows]
    widths = [
        max(len(header), *(len(line) for line in column))
        for header, column in zip(headers, zip(*rendered))
    ]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for line in rendered:
        print("  ".join(value.ljust(width) for value, width in zip(line, widths)))


def _build(parser_args: argparse.Namespace) -> Kiln:
    return Kiln(Store(Path(parser_args.db)))


def _emit(parser_args: argparse.Namespace, rows: list[dict], payload) -> None:
    if parser_args.json:
        _print_json(payload)
    else:
        _print_table(rows)


def cmd_manifest(args: argparse.Namespace) -> None:
    kiln = _build(args)
    manifest = kiln.register_manifest(
        args.seq, args.prev_seq, args.expected_groups)
    payload = asdict(manifest)
    _emit(args, [payload], payload)


def cmd_receive(args: argparse.Namespace) -> None:
    kiln = _build(args)
    result = kiln.receive(args.seq, args.group, args.content, args.event)
    payload = asdict(result)
    _emit(args, [payload], payload)


def cmd_pump(args: argparse.Namespace) -> None:
    kiln = _build(args)
    sequences = kiln.pump()
    payload = {"published": sequences, "count": len(sequences)}
    _emit(args, [payload], payload)
    if not args.json:
        if sequences:
            print(f"本次发布封单：{'、'.join(map(str, sequences))}")
        else:
            print("没有可发布的封单（缺口未补齐或存在卡点）")


def cmd_status(args: argparse.Namespace) -> None:
    kiln = _build(args)
    status = kiln.status()
    if args.json:
        _print_json(asdict(status))
        return
    print(f"当前发布水位：{status.watermark}")
    print(f"已发布封单：{'、'.join(map(str, status.published)) or '（无）'}")
    print()
    rows = [asdict(entry) for entry in status.queue]
    for row in rows:
        for key in ("expected_groups", "arrived_groups", "extra_groups",
                    "missing_groups", "conflicting_groups"):
            row[key] = "、".join(row[key])
        row["blocked"] = "是" if row["blocked"] else "否"
    _print_table(rows)
    blocker = kiln.head_blocker()
    print()
    print(f"卡点：{blocker.reason}" if blocker else "卡点：无，水位可继续推进")


def cmd_read(args: argparse.Namespace) -> None:
    kiln = _build(args)
    found = kiln.read(args.group, args.at)
    if found is None:
        payload = {"group_id": args.group, "visible": False, "reason": "无可见版本"}
        _emit(args, [payload], payload)
        return
    payload = {**asdict(found), "visible": not found.withdrawn}
    _emit(args, [payload], payload)


def cmd_groups(args: argparse.Namespace) -> None:
    kiln = _build(args)
    groups = kiln.list_groups()
    payload = {"groups": groups}
    _emit(args, [{"group_id": group} for group in groups], payload)


def cmd_withdraw(args: argparse.Namespace) -> None:
    kiln = _build(args)
    withdrawal = kiln.withdraw(args.group, args.reason)
    payload = asdict(withdrawal)
    _emit(args, [payload], payload)


def cmd_withdrawals(args: argparse.Namespace) -> None:
    kiln = _build(args)
    rows = [asdict(item) for item in kiln.list_withdrawals()]
    _emit(args, rows, {"withdrawals": rows})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiln", description="窑务有序入窑与只读副本台")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 台账路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("manifest", help="登记封单：序号 上笔序号 应到组...")
    p.add_argument("seq", type=int)
    p.add_argument("prev_seq", type=int)
    p.add_argument("expected_groups", nargs="+")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("receive", help="收来件：封单序号 组号 釉料内容")
    p.add_argument("seq", type=int)
    p.add_argument("group")
    p.add_argument("content")
    p.add_argument("--event", default=None, help="来件事件号，提供后可按事件号幂等重放")
    p.set_defaults(func=cmd_receive)

    p = sub.add_parser("pump", help="从水位下一笔起连续发布齐备的封单")
    p.set_defaults(func=cmd_pump)

    p = sub.add_parser("status", help="查看水位、队列与卡点")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("read", help="副本按水位读一组配方")
    p.add_argument("group")
    p.add_argument("--at", type=int, default=None, help="读取水位，缺省为当前水位")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("groups", help="列出已有发布版次的组")
    p.set_defaults(func=cmd_groups)

    p = sub.add_parser("withdraw", help="对已发布组撤版（追加墓碑）")
    p.add_argument("group")
    p.add_argument("reason")
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("withdrawals", help="列出撤版记录")
    p.set_defaults(func=cmd_withdrawals)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KilnError as exc:
        print(f"窑务错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
