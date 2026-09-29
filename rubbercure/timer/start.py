"""硫化计时启动。

计时启动的唯一前置条件是"模温确认已落盘且仍算数"。这条约束把温度回路与
计时回路绑在一起：没有落盘确认就启动计时，等于在模具还没到温的情况下
开始计时，制品必然欠硫。确认必须同时满足三件事才放行：

1. 已落盘（重启后按下盘内容恢复，内存里的口头确认不算数）；
2. 仍在有效期内（超过 ``confirm_ttl_s`` 的旧确认必须重写）；
3. 模具与传感器映射代际对得上（换模或重新登记映射后必须重新确认）。

启动动作在动作前强制校验，校验不通过直接拒绝并写审计。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..eventbus import Event, EventBus
from ..temp.persist import TempPersister

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免环
    from .timer import CureTimer


def start_cure(
    timer: "CureTimer",
    persister: TempPersister,
    mold_id: str,
    planned_s: float,
    bus: EventBus,
    *,
    product_code: str = "",
    generation: int = 0,
    mapping_generation: int | None = None,
) -> dict:
    """校验模温确认已落盘、未过期且属于当前模具代际后启动硫化计时。"""

    confirmation = persister.require_durable(mold_id, mapping_generation)
    started = timer.begin(mold_id, product_code, planned_s, generation)
    bus.publish(
        Event.of(
            "timer",
            "timer",
            "cure_started",
            subject=mold_id,
            severity="notice",
            planned_s=round(planned_s, 3),
            curve_generation=generation,
            mapping_generation=confirmation.generation,
            temp_confirmation_digest=confirmation.digest[:12],
        )
    )
    return started
