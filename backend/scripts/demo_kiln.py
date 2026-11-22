"""窑务有序入库的可复现演示。

    python -I scripts/demo_kiln.py            # 使用临时库，跑完即弃
    python -I scripts/demo_kiln.py ./work/demo-kiln.db

剧本覆盖：乱序来件、缺口拦截与旧水位读取、重发幂等、连续整笔发布、
按水位查配方、撤版与恢复、冲突卡笔、中断重开续跑。
"""

import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from src.services.kiln import Kiln  # noqa: E402
from src.services.kiln.store import Store  # noqa: E402


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main(db_path: str | None) -> None:
    if db_path is None:
        db_path = str(Path(tempfile.mkdtemp()) / "demo-kiln.db")
    print(f"台账文件：{db_path}")
    kiln = Kiln(Store(db_path))

    section("登记封单（注明上笔序号与本笔应到的组）")
    for seq, groups in ((1, ["青岚组", "蜜灰组"]), (2, ["青岚组"]), (3, ["青岚组"])):
        print(kiln.register_manifest(seq, seq - 1, groups))

    section("乱序来件：先到第 3 笔和第 1 笔的蜜灰组")
    print(kiln.receive(3, "青岚组", "青岚釉｜长石 56 g", "evt-3-青岚"))
    print(kiln.receive(1, "蜜灰组", "蜜灰釉｜长石 55 g", "evt-1-蜜灰"))
    print("pump ->", kiln.pump(), "（第 1 笔缺青岚组，后笔不可越过缺口）")
    blocker = kiln.head_blocker()
    print("卡点 ->", blocker.reason)

    section("缺件期间照常查旧水位（水位 0：无可见版本）")
    print("read 青岚组 ->", kiln.read("青岚组"))

    section("补齐缺口，整笔连续切换 1 → 2 → 3")
    print(kiln.receive(1, "青岚组", "青岚釉｜长石 60 g", "evt-1-青岚"))
    print(kiln.receive(2, "青岚组", "青岚釉｜长石 58 g", "evt-2-青岚"))
    print("pump ->", kiln.pump())
    print("当前水位 ->", kiln.status().watermark)

    section("重跑不重复入库：重放事件号与裸来件均幂等")
    print(kiln.receive(1, "青岚组", "青岚釉｜长石 60 g", "evt-1-青岚"))
    print(kiln.receive(2, "青岚组", "青岚釉｜长石 58 g"))
    print("pump ->", kiln.pump())

    section("副本按水位查配方")
    print("水位 1 ->", kiln.read("青岚组", at_seq=1))
    print("当前水位 ->", kiln.read("青岚组"))

    section("撤版：当前读缺席，历史水位仍读到原文")
    print(kiln.withdraw("青岚组", "班末撤下待核准"))
    print("当前读 ->", kiln.read("青岚组"))
    print("水位 1 ->", kiln.read("青岚组", at_seq=1))

    section("恢复写入：第 4 笔发布后重新可见")
    print(kiln.register_manifest(4, 3, ["青岚组"]))
    print(kiln.receive(4, "青岚组", "青岚釉｜长石 54 g（恢复）", "evt-4-青岚"))
    print("pump ->", kiln.pump())
    print("当前读 ->", kiln.read("青岚组"))

    section("冲突卡笔：同组来件内容对不上，第 5 笔卡住且已发布版次不变")
    print(kiln.register_manifest(5, 4, ["褐砂组"]))
    print(kiln.receive(5, "褐砂组", "褐砂釉｜长石 66 g", "evt-5-甲"))
    print(kiln.receive(5, "褐砂组", "褐砂釉｜长石 62 g（错版）", "evt-5-乙"))
    print("pump ->", kiln.pump())
    print("卡点 ->", kiln.head_blocker().reason)
    print("已发布水位保持 ->", kiln.status().watermark)

    section("中断续跑：重新打开同一台账文件")
    reopened = Kiln(Store(db_path))
    status = reopened.status()
    print("水位 ->", status.watermark, "| 已发布 ->", status.published,
          "| 队列卡点数 ->", len(status.blockers))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
