"""窑务模块的领域错误。"""


class KilnError(ValueError):
    """输入或操作不满足窑务规约。"""


class SequenceGapError(KilnError):
    """封单的上笔序号与已登记台账衔接不上。"""
