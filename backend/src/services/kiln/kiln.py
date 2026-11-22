"""窑务有序入库的领域门面。

规约要点（对应 BUSINESS.md「窑务规约」）：

1. 封单按序号衔接：每笔注明上笔序号，重发同号封单内容必须一致。
2. 来件可乱序、可重发；内容完全一致的重发幂等忽略；同组内容对不上则该笔卡死，
   冲突留痕但已发布版次保持原样。
3. 缺件照常收件并可查旧水位；只有从水位下一笔开始连续齐备的封单才能发布，
   后笔不能越过缺口；发布整笔原子切换。
4. 中断后续跑：所有状态落 SQLite，重跑同一批来件不会重复入库。
5. 副本按水位读配方、查撤版，并能看清卡在哪一笔。
"""

import hashlib
import json

from src.utils.time import utc_time

from .errors import KilnError, SequenceGapError
from .store import Store
from .types import (
    Block,
    GroupContent,
    IngestResult,
    Manifest,
    QueueEntry,
    Status,
    Withdrawal,
)

MAX_SEQUENCE = 2**53 - 1


class Kiln:
    """窑务台账与只读副本的统一入口。"""

    def __init__(self, store: Store):
        self.store = store

    # -- 封单登记 ----------------------------------------------------------

    def register_manifest(
        self, seq: int, prev_seq: int, expected_groups: list[str] | tuple[str, ...],
    ) -> Manifest:
        """登记一张封单（幂等：同序号重发且内容一致时返回原封单）。"""
        seq = self._sequence(seq, "封单序号", 1)
        prev_seq = self._sequence(prev_seq, "上笔序号", 0)
        groups = self._groups(expected_groups, "本笔应到的组")
        payload = json.dumps(list(groups), ensure_ascii=False)

        with self.store.transaction() as conn:
            row = self.store.get_manifest(conn, seq)
            if row is not None:
                if row["prev_seq"] != prev_seq or row["expected_groups"] != payload:
                    raise KilnError(
                        f"封单 {seq} 已登记，重发内容与原封单不一致，"
                        "已发布与已收件台账保持原样"
                    )
                return Manifest(seq, prev_seq, groups)
            if prev_seq >= seq:
                raise SequenceGapError(
                    f"封单 {seq} 的上笔序号 {prev_seq} 必须小于本笔序号"
                )
            predecessor = self.store.get_manifest(conn, prev_seq)
            if prev_seq != 0 and predecessor is None:
                raise SequenceGapError(
                    f"封单 {seq} 注明的上笔序号 {prev_seq} 尚未登记，"
                    "请先补齐上一笔封单"
                )
            self.store.insert_manifest(
                conn, seq, prev_seq, payload, utc_time())
            return Manifest(seq, prev_seq, groups)

    # -- 来件收件 ----------------------------------------------------------

    def receive(
        self, seq: int, group_id: str, content: str, event_id: str | None = None,
    ) -> IngestResult:
        """收一件来件。乱序、缺件都允许；重发幂等，冲突留痕并卡住该笔。"""
        seq = self._sequence(seq, "封单序号", 1)
        group_id = self._text(group_id, "组号")
        content = self._text(content, "釉料内容")
        if event_id is not None:
            event_id = self._text(event_id, "来件事件号")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        with self.store.transaction() as conn:
            # 事件级幂等：同一事件号重放直接返回首次落库的结果，不重复入库。
            if event_id is not None:
                logged = self.store.logged_event(conn, event_id)
                if logged is not None:
                    if (logged["seq"], logged["group_id"], logged["content_hash"]) != (
                        seq, group_id, content_hash,
                    ):
                        raise KilnError(
                            f"事件号 {event_id} 已用于另一笔来件，事件号不得复用"
                        )
                    return self._ingest_result(
                        conn, seq, group_id, content_hash,
                        accepted=False, duplicate=True, conflict=False)

            if self.store.is_published(conn, seq):
                # 已发布版次保持原样，迟到的重发只做幂等确认。
                if event_id is not None:
                    self.store.log_event(
                        conn, event_id, seq, group_id, content_hash, utc_time())
                return self._ingest_result(
                    conn, seq, group_id, content_hash,
                    accepted=False, duplicate=True, conflict=False)

            manifest = self.store.get_manifest(conn, seq)
            if manifest is None:
                raise KilnError(f"封单 {seq} 尚未登记，无法接收来件")
            expected = group_id in json.loads(manifest["expected_groups"])

            existing = self.store.get_delivery_hashes(conn, seq, group_id)
            if content_hash in existing:
                if event_id is not None:
                    self.store.log_event(
                        conn, event_id, seq, group_id, content_hash, utc_time())
                return self._ingest_result(
                    conn, seq, group_id, content_hash,
                    accepted=False, duplicate=True, conflict=False)

            conflict = bool(existing)
            if conflict:
                # 同组来件内容对不上：冲突内容留痕，该笔封单卡在本序号。
                self.store.record_conflict(
                    conn, seq, group_id, content_hash, content, utc_time())
            else:
                self.store.insert_delivery(
                    conn, seq, group_id, content, content_hash,
                    event_id, expected, utc_time())
            if event_id is not None:
                self.store.log_event(
                    conn, event_id, seq, group_id, content_hash, utc_time())
            return self._ingest_result(
                conn, seq, group_id, content_hash,
                accepted=not conflict, duplicate=False, conflict=conflict)

    def _ingest_result(
        self, conn, seq, group_id, content_hash,
        accepted, duplicate, conflict,
    ) -> IngestResult:
        manifest = self.store.get_manifest(conn, seq)
        expected = manifest is not None and group_id in json.loads(
            manifest["expected_groups"])
        return IngestResult(
            seq=seq, group_id=group_id, content_hash=content_hash,
            accepted=accepted, duplicate=duplicate,
            conflict=conflict, expected=expected)

    # -- 整笔发布 ----------------------------------------------------------

    def pump(self) -> list[int]:
        """从水位下一笔开始连续发布齐备且无冲突的封单，返回本次新发布序号。

        每个序号一个事务：提交即生效，中断后再次调用从同一水位续跑，
        已发布的封单不会重复入库。
        """
        published_now: list[int] = []
        while True:
            with self.store.transaction() as conn:
                watermark = self.store.watermark(conn)
                candidate = watermark + 1
                manifest = self.store.get_manifest(conn, candidate)
                if manifest is None:
                    return published_now  # 缺口：后笔不能越过
                entry = self._build_entry(conn, manifest)
                if entry.blocked:
                    return published_now

                deliveries = self.store.list_deliveries(conn, candidate)
                chosen: dict[str, object] = {}
                for row in deliveries:
                    if row["group_id"] in entry.expected_groups:
                        chosen[row["group_id"]] = row
                published_at = utc_time()
                for group_id in entry.expected_groups:
                    row = chosen[group_id]
                    self.store.insert_edition(
                        conn, candidate, group_id, row["content"],
                        row["content_hash"], published_at)
                self.store.mark_published(conn, candidate, published_at)
                published_now.append(candidate)

    # -- 台账全景与卡点 -----------------------------------------------------

    def status(self) -> Status:
        with self.store.transaction() as conn:
            watermark = self.store.watermark(conn)
            published = tuple(self.store.published_sequences(conn))
            entries = [
                self._build_entry(conn, row)
                for row in self.store.list_manifests(conn)
                if not self.store.is_published(conn, row["seq"])
            ]
            entries.sort(key=lambda entry: entry.seq)
            blockers = tuple(entry for entry in entries if entry.blocked)
            return Status(
                watermark=watermark, published=published,
                queue=tuple(entries), blockers=blockers)

    def head_blocker(self) -> Block | None:
        """返回拦住水位推进的第一笔（可能是缺件或冲突）。"""
        status = self.status()
        if not status.queue:
            return None
        head = status.queue[0]
        if head.seq != status.watermark + 1:
            return None
        if not head.blocked:
            return None
        if head.conflicting_groups:
            reason = (
                f"封单 {head.seq} 的组 "
                f"{'、'.join(head.conflicting_groups)} 存在对不上的来件内容，"
                "该笔卡在冲突上"
            )
        else:
            reason = (
                f"封单 {head.seq} 尚缺来件组：{'、'.join(head.missing_groups)}，"
                "后笔不能越过该缺口"
            )
        return Block(
            seq=head.seq, missing_groups=head.missing_groups,
            conflicting_groups=head.conflicting_groups, reason=reason)

    def _build_entry(self, conn, manifest_row) -> QueueEntry:
        seq = manifest_row["seq"]
        expected = tuple(json.loads(manifest_row["expected_groups"]))
        delivery_rows = self.store.list_deliveries(conn, seq)
        arrived = tuple(sorted({row["group_id"] for row in delivery_rows}))
        extra = tuple(sorted(set(arrived) - set(expected)))
        conflict_rows = self.store.list_conflicts(conn, seq)
        conflicting = tuple(sorted({row["group_id"] for row in conflict_rows}))
        missing = tuple(g for g in expected if g not in arrived)
        blocked = bool(missing or conflicting)
        return QueueEntry(
            seq=seq, prev_seq=manifest_row["prev_seq"],
            expected_groups=expected, arrived_groups=arrived,
            extra_groups=extra, missing_groups=missing,
            conflicting_groups=conflicting, blocked=blocked)

    # -- 只读副本：按水位查配方 ----------------------------------------------

    def read(self, group_id: str, at_seq: int | None = None) -> GroupContent | None:
        """按水位读取一组配方；at_seq 缺省取当前水位。

        命中撤版标记时返回 withdrawn=True 且 content 为空；
        水位内没有任何版次时返回 None（无可见版本）。
        """
        group_id = self._text(group_id, "组号")
        with self.store.transaction() as conn:
            watermark = self.store.watermark(conn)
            if at_seq is None:
                at_seq = watermark
            at_seq = self._sequence(at_seq, "读取水位", 0)
            if at_seq > watermark:
                raise KilnError(
                    f"读取水位 {at_seq} 高于当前发布水位 {watermark}，"
                    "副本不能读取未发布的版次"
                )
            edition = self.store.edition_at(conn, group_id, at_seq)
            if edition is None:
                return None
            withdrawal = self.store.withdrawal_at(conn, group_id, at_seq)
            if withdrawal is not None and withdrawal["seq"] >= edition["seq"]:
                return GroupContent(
                    group_id=group_id, seq=edition["seq"],
                    content="", content_hash=edition["content_hash"],
                    withdrawn=True)
            return GroupContent(
                group_id=group_id, seq=edition["seq"],
                content=edition["content"],
                content_hash=edition["content_hash"], withdrawn=False)

    def list_groups(self) -> list[str]:
        with self.store.transaction() as conn:
            return self.store.list_groups(conn)

    # -- 撤版 --------------------------------------------------------------

    def withdraw(self, group_id: str, reason: str) -> Withdrawal:
        """对已发布组追加撤版墓碑；版次原文保留，只读视图转为不可见。"""
        group_id = self._text(group_id, "组号")
        reason = self._text(reason, "撤版原因")
        with self.store.transaction() as conn:
            edition = self.store.latest_edition(conn, group_id)
            if edition is None:
                raise KilnError(f"组 {group_id} 没有已发布版次，不能撤版")
            seq = edition["seq"]
            marker = self.store.withdrawal_at(conn, group_id, self.store.watermark(conn))
            if marker is not None and marker["seq"] == seq:
                return Withdrawal(
                    group_id=group_id, seq=seq,
                    reason=marker["reason"], created_at=marker["created_at"])
            created_at = utc_time()
            self.store.insert_withdrawal(conn, group_id, seq, reason, created_at)
            return Withdrawal(
                group_id=group_id, seq=seq, reason=reason, created_at=created_at)

    def list_withdrawals(self) -> list[Withdrawal]:
        with self.store.transaction() as conn:
            return [
                Withdrawal(
                    group_id=row["group_id"], seq=row["seq"],
                    reason=row["reason"], created_at=row["created_at"])
                for row in self.store.list_withdrawals(conn)
            ]

    # -- 校验辅助 ----------------------------------------------------------

    @staticmethod
    def _sequence(value: object, label: str, minimum: int) -> int:
        if type(value) is not int or not minimum <= value <= MAX_SEQUENCE:
            raise KilnError(f"{label}须为 {minimum} 至 {MAX_SEQUENCE} 的整数")
        return value

    @staticmethod
    def _text(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise KilnError(f"{label}须为非空文本")
        return value.strip()

    @staticmethod
    def _groups(value: object, label: str) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or not value:
            raise KilnError(f"{label}须为非空数组")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise KilnError(f"{label}中的组号须为非空文本")
            item = item.strip()
            if item in seen:
                raise KilnError(f"{label}中的组号 {item} 重复")
            seen.add(item)
            cleaned.append(item)
        return tuple(cleaned)
