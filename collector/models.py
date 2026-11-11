"""采集计划与交接单的数据模型。

交接单字段即对外展示内容，设计上只含计划与页面元数据：
不含 cookie、口令或页面内的合成私密字段。
"""
from dataclasses import asdict, dataclass, field

# 计划状态
PLAN_ACTIVE = "active"                    # 可运行
PLAN_AWAITING = "awaiting_human"          # 已暂停，等待人工确认
PLAN_NEEDS_RECONFIRM = "needs_reconfirm"  # 恢复核对发现条件变更，需重新确认
PLAN_COMPLETED = "completed"
PLAN_CANCELLED = "cancelled"

# 交接单状态（一次性：pending -> resuming -> consumed）
TICKET_PENDING = "pending"
TICKET_RESUMING = "resuming"
TICKET_CONSUMED = "consumed"
TICKET_EXPIRED = "expired"
TICKET_CANCELLED = "cancelled"


@dataclass
class Plan:
    """采集计划：绑定计划编号、工作空间、分类、用户与可见资料范围。"""

    plan_id: str
    workspace: str
    category: str
    user: str
    scope: list
    read: dict = field(default_factory=dict)  # 资料编号 -> 读取时的版本
    status: str = PLAN_ACTIVE
    revision: int = 1
    created_at: float = 0.0

    @property
    def remaining(self):
        return [doc_id for doc_id in self.scope if doc_id not in self.read]

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass
class HandoffTicket:
    """交接单：遇人工确认页时输出，一次性使用，可移交他人引用 handoff_id 恢复。"""

    handoff_id: str
    plan_id: str
    workspace: str
    category: str
    user: str
    stopped_url: str
    reason: str
    completed: list
    remaining: list
    created_at: float
    expires_at: float
    status: str = TICKET_PENDING
    result: dict | None = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)
