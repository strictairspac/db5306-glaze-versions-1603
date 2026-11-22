"""窑务有序入库的规约测试。

直接运行：
    python -m unittest discover -s backend/tests -v
或：
    python -I backend/tests/test_kiln.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from src.services.kiln import Kiln, KilnError, SequenceGapError  # noqa: E402
from src.services.kiln.store import Store  # noqa: E402


class KilnTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "kiln.db"
        self.kiln = Kiln(Store(self.db_path))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def seed_manifests(self, *specs) -> None:
        for seq, groups in specs:
            self.kiln.register_manifest(seq, seq - 1, list(groups))


class OutOfOrderTests(KilnTestBase):
    def test_arrivals_out_of_order_then_whole_batch_switches(self) -> None:
        self.seed_manifests((1, ["A", "B"]))
        # 先到 B 后到 A，乱序收件期间只能查旧水位 0。
        self.kiln.receive(1, "B", "釉料B-v1", "evt-1b")
        self.assertEqual(self.kiln.status().watermark, 0)
        self.assertIsNone(self.kiln.read("A"))
        self.kiln.receive(1, "A", "釉料A-v1", "evt-1a")

        published = self.kiln.pump()
        self.assertEqual(published, [1])
        status = self.kiln.status()
        self.assertEqual(status.watermark, 1)
        self.assertEqual(self.kiln.read("A").content, "釉料A-v1")
        self.assertEqual(self.kiln.read("B").content, "釉料B-v1")
        self.assertEqual(status.blockers, ())

    def test_pump_is_idempotent_after_publish(self) -> None:
        self.seed_manifests((1, ["A"]))
        self.kiln.receive(1, "A", "v1")
        self.assertEqual(self.kiln.pump(), [1])
        self.assertEqual(self.kiln.pump(), [])
        # 版次只有一条，重跑 pump 不会重复入库。
        with self.kiln.store.transaction() as conn:
            editions = conn.execute("SELECT COUNT(*) AS n FROM editions").fetchone()["n"]
        self.assertEqual(editions, 1)


class GapTests(KilnTestBase):
    def test_missing_piece_blocks_later_batches(self) -> None:
        self.seed_manifests((1, ["A"]), (2, ["A"]), (3, ["A"]))
        # 第 2、3 笔齐备，但第 1 笔缺件，后笔不能越过缺口。
        self.kiln.receive(2, "A", "v2")
        self.kiln.receive(3, "A", "v3")
        self.assertEqual(self.kiln.pump(), [])
        blocker = self.kiln.head_blocker()
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.seq, 1)
        self.assertEqual(blocker.missing_groups, ("A",))

        # 缺件期间副本照常查旧水位（水位 0，什么都读不到）。
        self.assertIsNone(self.kiln.read("A"))

        # 补齐缺口后，连续三笔一次性依次切换。
        self.kiln.receive(1, "A", "v1")
        self.assertEqual(self.kiln.pump(), [1, 2, 3])
        self.assertEqual(self.kiln.status().watermark, 3)
        self.assertIsNone(self.kiln.head_blocker())

    def test_hole_in_sequence_halts_at_first_gap(self) -> None:
        self.seed_manifests((1, ["A"]), (2, ["A"]), (3, ["A"]))
        self.kiln.receive(1, "A", "v1")
        # 第 2 笔永远缺件，第 3 笔即使齐备也不能越过。
        self.kiln.receive(3, "A", "v3")
        self.assertEqual(self.kiln.pump(), [1])
        blocker = self.kiln.head_blocker()
        self.assertEqual(blocker.seq, 2)
        self.assertIn("尚缺来件组", blocker.reason)
        # 水位停在 1，第 3 笔对副本不可见。
        self.assertEqual(self.kiln.status().watermark, 1)
        with self.assertRaises(KilnError):
            self.kiln.read("A", at_seq=3)

    def test_old_watermark_reads_remain_available_while_waiting(self) -> None:
        self.seed_manifests((1, ["A"]), (2, ["A", "B"]))
        self.kiln.receive(1, "A", "v1")
        self.kiln.pump()
        # 第 2 笔缺 B，卡住；旧水位 1 的配方仍可正常复读。
        self.kiln.receive(2, "A", "v2")
        self.assertEqual(self.kiln.pump(), [])
        old = self.kiln.read("A", at_seq=1)
        self.assertEqual(old.content, "v1")
        self.assertEqual(old.seq, 1)
        # 当前水位仍是 1，读不到 v2。
        self.assertEqual(self.kiln.read("A").content, "v1")


class ConflictTests(KilnTestBase):
    def test_conflicting_group_content_sticks_the_batch(self) -> None:
        self.seed_manifests((1, ["A", "B"]))
        first = self.kiln.receive(1, "A", "original", "evt-a1")
        self.assertTrue(first.accepted)
        clash = self.kiln.receive(1, "A", "different", "evt-a2")
        self.assertTrue(clash.conflict)
        self.kiln.receive(1, "B", "b-content")
        self.assertEqual(self.kiln.pump(), [])
        blocker = self.kiln.head_blocker()
        self.assertEqual(blocker.seq, 1)
        self.assertEqual(blocker.conflicting_groups, ("A",))
        self.assertIn("对不上的来件内容", blocker.reason)

    def test_published_editions_are_immutable(self) -> None:
        self.seed_manifests((1, ["A"]), (2, ["A"]))
        self.kiln.receive(1, "A", "v1", "evt-1")
        self.kiln.pump()
        # 已发布版次保持原样：迟到的不同内容既不改写也不回退水位。
        late = self.kiln.receive(1, "A", "tampered", "evt-late")
        self.assertTrue(late.duplicate)
        self.assertFalse(late.conflict)
        self.assertEqual(self.kiln.read("A").content, "v1")
        self.assertEqual(self.kiln.status().watermark, 1)

    def test_duplicate_redelivery_is_idempotent(self) -> None:
        self.seed_manifests((1, ["A"]))
        first = self.kiln.receive(1, "A", "same", "evt-x")
        again = self.kiln.receive(1, "A", "same", "evt-y")
        self.assertTrue(first.accepted)
        self.assertTrue(again.duplicate)
        self.assertFalse(again.conflict)
        with self.kiln.store.transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE seq=1").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_event_id_replay_does_not_reinsert(self) -> None:
        self.seed_manifests((1, ["A"]))
        self.kiln.receive(1, "A", "payload", "evt-100")
        replay = self.kiln.receive(1, "A", "payload", "evt-100")
        self.assertTrue(replay.duplicate)
        with self.kiln.store.transaction() as conn:
            logs = conn.execute("SELECT COUNT(*) AS n FROM inbound_log").fetchone()["n"]
        self.assertEqual(logs, 1)

    def test_event_id_reused_for_different_payload_rejected(self) -> None:
        self.seed_manifests((1, ["A"]))
        self.kiln.receive(1, "A", "payload", "evt-1")
        with self.assertRaises(KilnError):
            self.kiln.receive(1, "A", "other", "evt-1")


class WatermarkReadTests(KilnTestBase):
    def test_historical_watermark_reads_and_resurrections(self) -> None:
        self.seed_manifests(
            (1, ["G"]), (2, ["G"]), (3, ["G"]), (4, ["G"]))
        self.kiln.receive(1, "G", "配方一")
        self.kiln.receive(2, "G", "配方二")
        self.kiln.receive(3, "G", "配方三")
        self.kiln.receive(4, "G", "配方四")
        self.kiln.pump()
        self.assertEqual(self.kiln.read("G", at_seq=1).content, "配方一")
        self.assertEqual(self.kiln.read("G", at_seq=2).content, "配方二")
        self.assertEqual(self.kiln.read("G").content, "配方四")
        with self.assertRaises(KilnError):
            self.kiln.read("G", at_seq=5)

    def test_unknown_group_has_no_visible_version(self) -> None:
        self.seed_manifests((1, ["A"]))
        self.kiln.receive(1, "A", "v")
        self.kiln.pump()
        self.assertIsNone(self.kiln.read("NO-SUCH"))


class WithdrawalTests(KilnTestBase):
    def test_withdraw_marks_tombstone_and_keeps_history(self) -> None:
        self.seed_manifests((1, ["A"]), (2, ["A"]))
        self.kiln.receive(1, "A", "v1")
        self.kiln.receive(2, "A", "v2")
        self.kiln.pump()
        marker = self.kiln.withdraw("A", "撤下待核准")
        self.assertEqual(marker.seq, 2)

        current = self.kiln.read("A")
        self.assertTrue(current.withdrawn)
        self.assertEqual(current.content, "")
        # 历史版次原文仍在，撤版前水位的读不受影响。
        old = self.kiln.read("A", at_seq=1)
        self.assertFalse(old.withdrawn)
        self.assertEqual(old.content, "v1")

        # 新一笔恢复写入后，最新读重新可见，墓碑不影响新版次。
        self.kiln.register_manifest(3, 2, ["A"])
        self.kiln.receive(3, "A", "v3")
        self.kiln.pump()
        restored = self.kiln.read("A")
        self.assertFalse(restored.withdrawn)
        self.assertEqual(restored.content, "v3")
        self.assertEqual(len(self.kiln.list_withdrawals()), 1)

    def test_withdraw_requires_published_edition_and_is_idempotent(self) -> None:
        with self.assertRaises(KilnError):
            self.kiln.withdraw("A", "没有版次")
        self.seed_manifests((1, ["A"]))
        self.kiln.receive(1, "A", "v1")
        self.kiln.pump()
        first = self.kiln.withdraw("A", "原因")
        second = self.kiln.withdraw("A", "原因")
        self.assertEqual(first, second)
        self.assertEqual(len(self.kiln.list_withdrawals()), 1)


class ManifestTests(KilnTestBase):
    def test_manifest_chain_must_be_registered_in_order(self) -> None:
        with self.assertRaises(SequenceGapError):
            self.kiln.register_manifest(2, 1, ["A"])
        self.kiln.register_manifest(1, 0, ["A"])
        self.kiln.register_manifest(2, 1, ["A"])  # 现在可以登记

    def test_manifest_redelivery_must_match(self) -> None:
        self.kiln.register_manifest(1, 0, ["A", "B"])
        # 同序号重发且一致：幂等。
        again = self.kiln.register_manifest(1, 0, ["A", "B"])
        self.assertEqual(again.expected_groups, ("A", "B"))
        # 同序号重发但内容对不上：拒绝，台账保持原样。
        with self.assertRaises(KilnError):
            self.kiln.register_manifest(1, 0, ["A", "C"])
        with self.assertRaises(KilnError):
            self.kiln.register_manifest(1, 0, ["A"])

    def test_invalid_inputs_rejected(self) -> None:
        with self.assertRaises(KilnError):
            self.kiln.register_manifest(0, 0, ["A"])
        with self.assertRaises(KilnError):
            self.kiln.register_manifest(1, 1, ["A"])
        with self.assertRaises(KilnError):
            self.kiln.register_manifest(1, 0, [])
        with self.assertRaises(KilnError):
            self.kiln.register_manifest(1, 0, ["A", "A"])
        with self.assertRaises(KilnError):
            self.kiln.receive(1, "A", "   ")

    def test_receive_before_manifest_rejected(self) -> None:
        with self.assertRaises(KilnError):
            self.kiln.receive(9, "A", "v")

    def test_unexpected_group_is_accepted_but_flagged(self) -> None:
        self.kiln.register_manifest(1, 0, ["A"])
        result = self.kiln.receive(1, "X", "计划外内容")
        self.assertFalse(result.expected)
        entry = next(iter(self.kiln.status().queue))
        self.assertEqual(entry.extra_groups, ("X",))
        self.assertEqual(entry.missing_groups, ("A",))
        # 计划外来件不替代应到组，缺口仍在。
        self.assertTrue(entry.blocked)


class ResumeTests(KilnTestBase):
    def test_reopen_store_resumes_without_duplicate_inserts(self) -> None:
        self.seed_manifests((1, ["A", "B"]), (2, ["A"]))
        self.kiln.receive(1, "A", "v1", "evt-a")
        self.kiln.receive(1, "B", "v1", "evt-b")
        self.kiln.pump()

        # 模拟进程中断：丢弃内存对象，用同一数据库文件重新打开续跑。
        reopened = Kiln(Store(self.db_path))
        self.assertEqual(reopened.status().watermark, 1)
        # 重放旧事件与旧来件不重复入库。
        reopened.receive(1, "A", "v1", "evt-a")
        reopened.receive(1, "B", "v1")
        reopened.receive(2, "A", "v2", "evt-2")
        self.assertEqual(reopened.pump(), [2])
        self.assertEqual(reopened.status().watermark, 2)
        with reopened.store.transaction() as conn:
            deliveries = conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
            editions = conn.execute(
                "SELECT COUNT(*) AS n FROM editions").fetchone()["n"]
        self.assertEqual(deliveries, 3)
        self.assertEqual(editions, 3)

    def test_crash_between_pump_iterations_resumes(self) -> None:
        """跨进程中断：每个序号独立事务，中断后重放输入得到同一结果。"""
        script = Path(self.tempdir.name) / "drive.py"
        db_file = Path(self.tempdir.name) / "crash.db"
        marker = Path(self.tempdir.name) / "stop"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(BACKEND_ROOT)!r})\n"
            "from pathlib import Path\n"
            "from src.services.kiln import Kiln\n"
            "from src.services.kiln.store import Store\n"
            f"kiln = Kiln(Store({str(db_file)!r}))\n"
            "for seq in range(1, 6):\n"
            "    kiln.register_manifest(seq, seq - 1, ['A', 'B'])\n"
            "    kiln.receive(seq, 'A', f'A-{seq}', f'evt-a-{seq}')\n"
            "    kiln.receive(seq, 'B', f'B-{seq}', f'evt-b-{seq}')\n"
            "published = kiln.pump()\n"
            f"if Path({str(marker)!r}).exists():\n"
            "    sys.exit(7)\n"
            "print(published)\n",
            encoding="utf-8",
        )
        marker.write_text("stop", encoding="utf-8")
        crashed = subprocess.run(
            [sys.executable, "-I", str(script)],
            capture_output=True, text=True, check=False)
        self.assertEqual(crashed.returncode, 7, crashed.stderr)

        # 中断后重新打开：已提交的发布不丢，重放全部来件不重复入库。
        marker.unlink()
        rerun = subprocess.run(
            [sys.executable, "-I", str(script)],
            capture_output=True, text=True, check=False)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        # 第二次运行时五封单都已发布，pump 只补做剩余事务且结果幂等。
        self.assertEqual(json.loads(rerun.stdout.strip()), [])

        checker = Kiln(Store(db_file))
        self.assertEqual(checker.status().watermark, 5)
        self.assertEqual(checker.read("A").content, "A-5")
        with checker.store.transaction() as conn:
            editions = conn.execute(
                "SELECT COUNT(*) AS n FROM editions").fetchone()["n"]
            deliveries = conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
            events = conn.execute(
                "SELECT COUNT(*) AS n FROM inbound_log").fetchone()["n"]
        self.assertEqual(editions, 10)       # 5 笔 × 2 组，无重复
        self.assertEqual(deliveries, 10)
        self.assertEqual(events, 10)


class CliTests(KilnTestBase):
    BOOTSTRAP = (
        "import sys; "
        f"sys.path.insert(0, {str(BACKEND_ROOT)!r}); "
        "from src.cli.kiln_cli import main; "
        "sys.exit(main())"
    )

    def run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", "-c", self.BOOTSTRAP,
             "--db", str(self.db_path), *argv],
            capture_output=True, text=True, check=False)

    def test_cli_end_to_end_json(self) -> None:
        result = self.run_cli("manifest", "1", "0", "A", "B")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("receive", "1", "B", "釉B", "--event", "e-b")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("receive", "1", "A", "釉A", "--event", "e-a")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("--json", "pump")
        self.assertEqual(json.loads(result.stdout)["published"], [1])
        result = self.run_cli("--json", "read", "A")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["content"], "釉A")
        self.assertFalse(payload["withdrawn"])

    def test_cli_reports_blocker(self) -> None:
        self.run_cli("manifest", "1", "0", "A")
        self.run_cli("manifest", "2", "1", "A")
        self.run_cli("receive", "2", "A", "v2")
        result = self.run_cli("pump")
        self.assertEqual(result.returncode, 0)
        status = self.run_cli("status")
        self.assertIn("卡点", status.stdout)
        self.assertIn("封单 1", status.stdout)

    def test_cli_withdraw_flow(self) -> None:
        self.run_cli("manifest", "1", "0", "A")
        self.run_cli("receive", "1", "A", "v1")
        self.run_cli("pump")
        result = self.run_cli("--json", "withdraw", "A", "停用")
        self.assertEqual(json.loads(result.stdout)["seq"], 1)
        result = self.run_cli("--json", "read", "A")
        self.assertTrue(json.loads(result.stdout)["withdrawn"])
        result = self.run_cli("withdrawals")
        self.assertIn("停用", result.stdout)

    def test_cli_domain_error_exit_code(self) -> None:
        result = self.run_cli("manifest", "2", "1", "A")
        self.assertEqual(result.returncode, 2)
        self.assertIn("窑务错误", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
