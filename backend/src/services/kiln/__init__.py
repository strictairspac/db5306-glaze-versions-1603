"""窑务有序入窑与副本只读查询。

术语对照（见 BUSINESS.md 中的窑务规约）：

- 封单（manifest）：配方室分组送出的一笔版次，注明上笔序号与本笔应到的组。
- 来件（delivery）：封单内某一组的实际釉料内容，可能乱序到达，允许重发。
- 水位（watermark）：已发布的最高封单序号；副本只能读到水位之内的版次。
- 发布（publish）：一笔封单的所有应到组补齐且无内容冲突后，整笔原子切换。
- 撤版（withdraw）：对已发布组追加撤版标记（墓碑），不改写历史版次。
"""

from .errors import KilnError, SequenceGapError
from .kiln import Kiln
from .store import Store

__all__ = ["Kiln", "Store", "KilnError", "SequenceGapError"]
