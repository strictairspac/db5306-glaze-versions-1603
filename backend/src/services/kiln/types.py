"""窑务模块对外暴露的不可变数据结构。"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Manifest:
    """一张封单：一笔版次的收件计划。"""

    seq: int
    prev_seq: int
    expected_groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Delivery:
    """一件来件：某笔封单中某个组实际送到的釉料内容。"""

    seq: int
    group_id: str
    content: str
    content_hash: str
    event_id: str | None


@dataclass(frozen=True, slots=True)
class IngestResult:
    """登记来件的结果。

    accepted=True 表示内容首次落库；duplicate=True 表示与已收到的来件完全一致，
    属于重发，直接幂等忽略；conflict=True 表示同组内容对不上，该笔封单被卡住。
    expected=False 表示该组不在本笔封单的应到清单内（照常收件，仅标记为计划外）。
    """

    seq: int
    group_id: str
    content_hash: str
    accepted: bool
    duplicate: bool
    conflict: bool
    expected: bool


@dataclass(frozen=True, slots=True)
class Block:
    """一笔封单不能发布的原因。"""

    seq: int
    missing_groups: tuple[str, ...]
    conflicting_groups: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """队列中一笔封单的收件进展。"""

    seq: int
    prev_seq: int
    expected_groups: tuple[str, ...]
    arrived_groups: tuple[str, ...]
    extra_groups: tuple[str, ...]
    missing_groups: tuple[str, ...]
    conflicting_groups: tuple[str, ...]
    blocked: bool


@dataclass(frozen=True, slots=True)
class GroupContent:
    """副本在某个水位读到的一组配方。"""

    group_id: str
    seq: int
    content: str
    content_hash: str
    withdrawn: bool


@dataclass(frozen=True, slots=True)
class Withdrawal:
    """一条撤版记录。"""

    group_id: str
    seq: int
    reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Status:
    """窑务台账全景：水位、队列与卡点。"""

    watermark: int
    published: tuple[int, ...]
    queue: tuple[QueueEntry, ...]
    blockers: tuple[Block, ...] = field(default_factory=tuple)
