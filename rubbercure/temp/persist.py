"""模温确认落盘。

硫化计时只能在本机确认模温已进入容差带、并且这份确认已经落盘之后启动。
"确认"是运行期状态，"落盘"是持久事实：控制器重启后按下盘内容恢复，因此
没有落盘记录的确认一律不算数。落盘采用原子替换，避免留下半份 JSON 让
重启后的判定落空。

确认不是永久凭据，落盘时同时写死三件事，任何一件对不上都必须重新确认：

1. **有效期**（``ttl_s``）：以挂钟计，``confirmed_at + ttl_s`` 之后自动失效，
   隔了很久再启动计时等于拿一份旧温度状态放行，制品必然欠硫；
2. **模具**（``mold_id``）：换模后旧模具的确认对新模具没有意义；
3. **映射代际**（``generation``）：同一台模具重新登记／换探头会产生新代际，
   旧代际下的确认同样作废。

重启恢复只按下盘内容重建，然后重新走一遍有效期、模具与代际判定——落盘只
证明"这份确认存在过"，不证明"它现在还算数"。早期未落 ``ttl_s`` 的文档
按有效期 0 处理，即重启即过期，强制重新确认。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..clock import Clock
from ..config import TempLimits
from ..errors import InterlockError
from ..eventbus import Event, EventBus
from ..store.jsonfile import JsonStore
from .read import MoldTempReading
from .tolerance import band_summary

STATE_KEY = "temp_confirmation"

# 早期落盘文档没有 ttl_s 字段：没有有效期信息的确认重启后一律不算数。
LEGACY_TTL_S = 0.0


@dataclass(frozen=True)
class TemperatureConfirmation:
    """一份已经落盘的模温确认。"""

    mold_id: str
    generation: int
    setpoint_c: float
    confirmed_c: float
    confirmed_at: float
    hold_s: float
    ttl_s: float
    digest: str = ""

    def expires_at(self) -> float:
        """有效期截止时刻（挂钟秒）。"""

        return self.confirmed_at + self.ttl_s

    def is_fresh(self, now: float) -> bool:
        """是否仍在有效期内：超过截止时刻即失效，临界时刻仍算数。

        ``ttl_s <= 0`` 表示这份确认没有有效时长（早期落盘文档即如此），
        任何时刻都不算数，重启后必须重新确认。
        """

        if self.ttl_s <= 0.0:
            return False
        return (now - self.confirmed_at) <= self.ttl_s

    def is_for(self, mold_id: str, generation: int | None = None) -> bool:
        """确认是否针对给定模具（以及给定映射代际）做出。"""

        if self.mold_id != mold_id:
            return False
        return generation is None or self.generation == generation

    def remaining_s(self, now: float) -> float:
        """剩余有效秒数；已过期返回 0。"""

        return max(0.0, self.expires_at() - now)

    def as_dict(self) -> dict:
        return {
            "mold_id": self.mold_id,
            "generation": self.generation,
            "setpoint_c": self.setpoint_c,
            "confirmed_c": round(self.confirmed_c, 3),
            "confirmed_at": round(self.confirmed_at, 3),
            "hold_s": round(self.hold_s, 3),
            "ttl_s": round(self.ttl_s, 3),
            "expires_at": round(self.expires_at(), 3),
            "digest": self.digest[:12],
        }


class TempPersister:
    """把模温确认写入原子存储，并对外提供"已落盘且仍算数"判定。"""

    def __init__(
        self,
        clock: Clock,
        bus: EventBus,
        store: JsonStore,
        limits: TempLimits,
    ) -> None:
        self._clock = clock
        self._bus = bus
        self._store = store
        self._limits = limits
        self._latest: TemperatureConfirmation | None = None
        self._lock = threading.RLock()

    def confirm(self, reading: MoldTempReading, hold_s: float) -> TemperatureConfirmation:
        """在模温进入容差带并保持足够时长后写下一份带有效期的确认。"""

        measured = reading.average_c()
        if not self._limits.within_tolerance(measured):
            raise InterlockError(
                f"模具 {reading.mold_id} 模温 {measured:.2f}℃ 未进入容差带，禁止确认",
                component="temp",
            )
        if hold_s < self._limits.confirm_hold_s:
            raise InterlockError(
                f"模具 {reading.mold_id} 模温保持 {hold_s:.1f}s 未达 {self._limits.confirm_hold_s:.1f}s",
                component="temp",
            )
        confirmed_at = round(self._clock.now(), 4)
        payload = {
            "mold_id": reading.mold_id,
            "generation": reading.generation,
            "setpoint_c": self._limits.setpoint_c,
            "confirmed_c": round(measured, 4),
            "confirmed_at": confirmed_at,
            "hold_s": round(float(hold_s), 4),
            "ttl_s": round(self._limits.confirm_ttl_s, 4),
        }
        digest = self._store.write(STATE_KEY, payload)
        confirmation = TemperatureConfirmation(
            mold_id=reading.mold_id,
            generation=reading.generation,
            setpoint_c=self._limits.setpoint_c,
            confirmed_c=measured,
            confirmed_at=confirmed_at,
            hold_s=hold_s,
            ttl_s=self._limits.confirm_ttl_s,
            digest=digest,
        )
        with self._lock:
            self._latest = confirmation
        detail = confirmation.as_dict()
        detail.update(
            band_summary(measured, self._limits.setpoint_c, self._limits.tolerance_c)
        )
        self._bus.publish(
            Event.of(
                "temp",
                "temp",
                "temp_confirmed",
                subject=STATE_KEY,
                severity="notice",
                **detail,
            )
        )
        return confirmation

    def latest(self) -> TemperatureConfirmation | None:
        with self._lock:
            return self._latest

    def stored(self) -> dict | None:
        """直接读回落盘内容，用于与内存视图对照。"""

        return self._store.read(STATE_KEY)

    def invalidate(self, reason: str = "换模") -> bool:
        """作废旧确认（换模／维护后调用）：清内存视图并删除落盘文档。

        返回是否真的有一份确认被作废。作废之后任何计时启动都必须重新确认。
        """

        with self._lock:
            stale = self._latest
            self._latest = None
            removed = self._store.remove(STATE_KEY)
        if stale is None and not removed:
            return False
        self._bus.publish(
            Event.of(
                "temp",
                "temp",
                "temp_confirmation_invalidated",
                subject=STATE_KEY,
                severity="warning",
                reason=reason,
                stale_mold=stale.mold_id if stale else None,
                stale_generation=stale.generation if stale else None,
            )
        )
        return True

    def is_durable(self) -> bool:
        """确认已落盘且未过期：内存有记录、磁盘有对应文档且仍在有效期内。

        只回答"有没有一份算数的落盘确认"；模具与代际是否匹配由
        :meth:`require_durable` 按计时对象再判一层。
        """

        with self._lock:
            latest = self._latest
            if latest is None:
                return False
            now = self._clock.now()
            fresh = latest.is_fresh(now)
        return bool(
            fresh
            and self._store.exists(STATE_KEY)
            and self._store.digest(STATE_KEY)
        )

    def require_durable(
        self,
        mold_id: str | None = None,
        generation: int | None = None,
    ) -> TemperatureConfirmation:
        """硫化计时在启动前必须确认模温已落盘、未过期且属于当前模具。

        ``mold_id`` 给定时校验确认对象，``generation`` 再给定时校验确认时的
        传感器映射代际——换模或重新登记映射后旧确认一律不放行。
        """

        with self._lock:
            latest = self._latest
            exists = latest is not None and self._store.exists(STATE_KEY)
            now = self._clock.now()
        if not exists:
            raise InterlockError("模温确认尚未落盘，禁止启动硫化计时", component="temp")
        assert latest is not None  # exists 已保证非空
        if not self._store.digest(STATE_KEY):
            raise InterlockError("模温确认落盘内容缺失，禁止启动硫化计时", component="temp")
        if not latest.is_fresh(now):
            raise InterlockError(
                f"模具 {latest.mold_id} 的模温确认已超过有效期 "
                f"{latest.ttl_s:.0f}s，必须重新确认",
                component="temp",
            )
        if mold_id is not None and not latest.is_for(mold_id, generation):
            if latest.mold_id != mold_id:
                raise InterlockError(
                    f"模温确认属于 {latest.mold_id}，与计时对象 {mold_id} 不一致，必须重新确认",
                    component="temp",
                )
            raise InterlockError(
                f"模温确认基于第 {latest.generation} 代映射，当前为第 {generation} 代，"
                "换模或重新登记映射后必须重新确认",
                component="temp",
            )
        return latest

    def restore(self) -> dict:
        """从磁盘恢复上次确认，并按下盘内容当场判定还算不算数。

        返回 ``restored``（有没有文档可恢复）与 ``valid``（恢复后是否仍在
        有效期内）。过期的确认保留为最近记录供快照展示，但不会通过
        :meth:`require_durable` 联锁；模具与映射代际则在计时启动时按当前
        模具再判一层。
        """

        payload = self._store.read(STATE_KEY)
        if not payload:
            return {"restored": False, "valid": False}
        try:
            ttl_s = float(payload.get("ttl_s", LEGACY_TTL_S))
            confirmation = TemperatureConfirmation(
                mold_id=str(payload["mold_id"]),
                generation=int(payload["generation"]),
                setpoint_c=float(payload["setpoint_c"]),
                confirmed_c=float(payload["confirmed_c"]),
                confirmed_at=float(payload["confirmed_at"]),
                hold_s=float(payload["hold_s"]),
                ttl_s=ttl_s,
                digest=self._store.digest(STATE_KEY),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InterlockError(f"模温确认落盘内容不完整: {exc}", component="temp") from exc
        now = self._clock.now()
        valid = confirmation.is_fresh(now)
        with self._lock:
            self._latest = confirmation
        self._bus.publish(
            Event.of(
                "temp",
                "temp",
                "temp_restored",
                subject=STATE_KEY,
                mold_id=confirmation.mold_id,
                generation=confirmation.generation,
                ttl_s=round(ttl_s, 3),
                valid=valid,
            )
        )
        return {"restored": True, "valid": valid}

    def snapshot(self) -> dict:
        with self._lock:
            latest = self._latest
            now = self._clock.now()
        stored = self.stored()
        return {
            "durable": self.is_durable(),
            "latest": latest.as_dict() if latest else None,
            "remaining_s": round(latest.remaining_s(now), 3) if latest else 0.0,
            "stored_mold": stored.get("mold_id") if stored else None,
        }
