# 窑务有序入窑（Kiln Ledger）

窑务从只读副本查釉料。配方室按分组送版次封单，来件可能乱序；模块保证缺件不越序、
补齐后整笔原子切换、冲突卡笔不改已发布版次、中断可续跑且重跑不重复入库。

代码位置：`backend/src/services/kiln/`，仅依赖 Python 3.11 标准库（SQLite）。

## 模型与规约映射

| 规约原话 | 实现 |
| --- | --- |
| 封单注明上笔序号和本笔应到的组 | `register_manifest(seq, prev_seq, expected_groups)`，`prev_seq` 必须已登记且小于 `seq`；同号重发内容须一致，否则报 `KilnError` 且台账不变 |
| 来件可能乱序 | `receive()` 不要求按序，按 `(seq, group_id)` 归位；`pump()` 只从水位下一笔开始连续发布 |
| 缺件时照常收件和查旧水位 | 缺组只进入队列的 `missing_groups`，不拒收其他来件；`read(group, at_seq)` 可查任意不超过当前水位的历史水位 |
| 补齐后整笔一次性切换 | `pump()` 对每个封单在**一个事务**内写入该笔全部组的版次并推进发布水位 |
| 后笔不能越过缺口 | 水位下一笔缺封单、缺件或有冲突即停，后续齐备封单留在队列 |
| 未发布一笔里同组内容对不上就卡在该笔 | 冲突内容写入 `delivery_conflicts` 留痕，`blocked=True`；原件保留在 `deliveries`，该笔不参与发布 |
| 已发布的版次保持原样 | 已发布序号的迟到来件一律按重发幂等处理，不产生冲突、不改版次、不回退水位 |
| 中断后能续跑，重跑不重复入库 | 全部状态在 SQLite；每个序号独立事务。去重有两层：内容哈希唯一约束 `(seq, group_id, content_hash)` 与可选事件号 `inbound_log` |
| 副本按水位查配方和撤版 | `read()` 取该组 `seq ≤ 水位` 的最后一条版次；`withdraw()` 追加墓碑，命中墓碑时 `withdrawn=True`、内容为空，历史版次原文保留 |
| 看清卡在哪笔 | `status()` 给出水位、队列进展与全部卡点；`head_blocker()` 返回拦住水位的第一笔及原因 |

## 存储表（SQLite，自动建表）

- `manifests`：封单计划（序号、上笔序号、应到组 JSON）。
- `deliveries`：已收录来件，`(seq, group_id, content_hash)` 唯一。
- `delivery_conflicts`：对不上的来件内容留痕，仅供追溯。
- `published` / `editions`：发布序号与只读副本可见的版次（append-only）。
- `withdrawals`：撤版墓碑，不改写 `editions`。
- `inbound_log`：可选事件号去重日志，事件号不得复用为不同内容。

## 命令行副本台

```sh
python -I -m src.cli.kiln_cli --db ./work/kiln.db manifest 1 0 青岚组 蜜灰组
python -I -m src.cli.kiln_cli --db ./work/kiln.db receive 1 蜜灰组 "蜜灰釉配方" --event evt-1
python -I -m src.cli.kiln_cli --db ./work/kiln.db pump       # 连续发布齐备封单
python -I -m src.cli.kiln_cli --db ./work/kiln.db status     # 水位、队列、卡点
python -I -m src.cli.kiln_cli --db ./work/kiln.db read 青岚组 --at 1
python -I -m src.cli.kiln_cli --db ./work/kiln.db withdraw 青岚组 "班末撤下"
python -I -m src.cli.kiln_cli --db ./work/kiln.db withdrawals
```

> 在仓库根目录运行时把 `src.cli.kiln_cli` 换成带 `PYTHONPATH=backend` 的调用，
> 或直接 `cd backend` 后执行。所有命令支持 `--json` 输出。

可复现剧本：

```sh
python -I backend/scripts/demo_kiln.py
```

## 测试

```sh
python -I backend/tests/test_kiln.py
# 或
python -m unittest discover -s backend/tests -v
```

25 个用例覆盖：乱序切换、缺口拦截、旧水位读取、冲突卡笔、已发布不可变、
内容/事件号双层幂等、撤版与恢复、重开续跑、跨进程中断重放、CLI 全链路。

## 边界与取舍

- 冲突按规约为**终态卡笔**：模块不提供静默覆盖或自动选版；处置流程（人工裁决后
  补发新封单）属于上游职责，冲突证据已完整留痕。
- 计划外来件（组号不在封单清单）照常收件，标记 `expected=False` 并计入 `extra_groups`，
  不替代任何应到组。
- 读取水位只接受 `0 … 2^53-1` 且不得高于当前发布水位；未发布内容对副本永远不可见。
- 撤版墓碑挂在该组当时最新版次的序号上；后续恢复写入的新版次自动重新可见，
  撤版前水位的历史读不受影响。
