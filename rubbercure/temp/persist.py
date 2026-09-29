"""模温确认落盘。

硫化计时只能在本机确认模温已进入容差带、并且这份确认已经落盘之后启动。
"确认"是运行期状态，"落盘"是持久事实：控制器重启后按下盘内容恢复，因此
没有落盘记录的确认一律不算数。落盘采用原子替换，避免留下半份 JSON 让
重启后的判定落空。

确认不是永久有效的：每份确认都带一个绝对过期时刻 ``expires_at``（确认时刻
加上配置的有效期），超期即作废，必须重新确认。换模（装上新的在机模具）也
会立刻作废旧确认。重启后按下盘那份记录重新判定——落盘时还没过有效期就
继续算数，已经过期（包括旧版本没有过期时刻的历史文档）就必须重新确认。
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


@dataclass(frozen=True)
class TemperatureConfirmation:
    """一份已经落盘的模温确认。"""

    mold_id: str
    generation: int
    setpoint_c: float
    confirmed_c: float
    confirmed_at: float
    hold_s: float
    expires_at: float
    digest: str = ""

    def is_expired(self, now: float) -> bool:
        """到没到绝对过期时刻。"""

        return now > self.expires_at

    def remaining_s(self, now: float) -> float:
        """剩余有效期（秒），已过期时不大于零。"""

        return self.expires_at - now

    def as_dict(self) -> dict:
        return {
            "mold_id": self.mold_id,
            "generation": self.generation,
            "setpoint_c": self.setpoint_c,
            "confirmed_c": round(self.confirmed_c, 3),
            "confirmed_at": round(self.confirmed_at, 3),
            "hold_s": round(self.hold_s, 3),
            "expires_at": round(self.expires_at, 3),
            "digest": self.digest[:12],
        }


class TempPersister:
    """把模温确认写入原子存储，并对外提供"已落盘且仍有效"判定。"""

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
        """在模温进入容差带并保持足够时长后写下一份确认。"""

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
            "expires_at": round(confirmed_at + self._limits.confirm_max_age_s, 4),
        }
        digest = self._store.write(STATE_KEY, payload)
        confirmation = TemperatureConfirmation(
            mold_id=reading.mold_id,
            generation=reading.generation,
            setpoint_c=self._limits.setpoint_c,
            confirmed_c=measured,
            confirmed_at=payload["confirmed_at"],
            hold_s=hold_s,
            expires_at=payload["expires_at"],
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
                max_age_s=self._limits.confirm_max_age_s,
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

    def invalidate(self, reason: str = "模温确认失效") -> bool:
        """作废旧确认并删掉落盘文档；原本就没有有效确认时返回 False。

        换模时由机台调用：上一副模具的确认绝不能留给下一副模具。超期不删
        文档（保留审计痕迹，判定时按过期处理），因此本方法只服务于"换模／
        人工作废"这类与时间无关的失效。
        """

        with self._lock:
            confirmation = self._latest
            removed = self._store.remove(STATE_KEY)
            self._latest = None
            if confirmation is None and not removed:
                return False
            detail = {
                "mold_id": confirmation.mold_id if confirmation else None,
                "reason": reason,
            }
        self._bus.publish(
            Event.of(
                "temp",
                "temp",
                "temp_confirmation_invalidated",
                subject=STATE_KEY,
                severity="warning",
                **detail,
            )
        )
        return True

    def is_durable(self) -> bool:
        """确认已落盘、仍在有效期内：内存有记录、磁盘文档与摘要都在。"""

        with self._lock:
            latest = self._latest
            if latest is None:
                return False
            if not (self._store.exists(STATE_KEY) and self._store.digest(STATE_KEY)):
                return False
            return not latest.is_expired(self._clock.now())

    def require_durable(self) -> TemperatureConfirmation:
        """硫化计时在启动前必须确认模温已落盘且未超期。"""

        with self._lock:
            latest = self._latest
            if latest is None:
                raise InterlockError("模温确认尚未落盘，禁止启动硫化计时", component="temp")
            if not (self._store.exists(STATE_KEY) and self._store.digest(STATE_KEY)):
                raise InterlockError(
                    "模温确认缺少落盘文档，禁止启动硫化计时", component="temp"
                )
            if latest.is_expired(self._clock.now()):
                raise InterlockError(
                    f"模具 {latest.mold_id} 的模温确认已超期 "
                    f"（有效期 {self._limits.confirm_max_age_s:.0f}s），必须重新确认",
                    component="temp",
                )
            return latest

    def restore(self) -> bool:
        """从磁盘恢复上次确认，并按下盘的过期时刻重新判定还算不算数。

        磁盘上没有文档时返回 False；文档过期时恢复到内存供快照查看，但
        ``is_durable`` 仍为 False，必须重新确认；旧版本没有过期时刻的历史
        文档无法判定有效期，直接作废并要求重新确认。
        """

        payload = self._store.read(STATE_KEY)
        if not payload:
            return False
        try:
            confirmation = TemperatureConfirmation(
                mold_id=str(payload["mold_id"]),
                generation=int(payload["generation"]),
                setpoint_c=float(payload["setpoint_c"]),
                confirmed_c=float(payload["confirmed_c"]),
                confirmed_at=float(payload["confirmed_at"]),
                hold_s=float(payload["hold_s"]),
                expires_at=float(payload["expires_at"]),
                digest=self._store.digest(STATE_KEY),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # 旧版本落盘的确认没有过期时刻，重启后无法证明它仍在有效期内：
            # 删掉历史文档并按"从未确认"处理，强制现场重新确认。
            self._store.remove(STATE_KEY)
            with self._lock:
                self._latest = None
            self._bus.publish(
                Event.of(
                    "temp",
                    "temp",
                    "temp_confirmation_discarded",
                    subject=STATE_KEY,
                    severity="warning",
                    reason=f"落盘确认缺少有效期字段: {exc}",
                )
            )
            return False
        with self._lock:
            self._latest = confirmation
        expired = confirmation.is_expired(self._clock.now())
        self._bus.publish(
            Event.of(
                "temp",
                "temp",
                "temp_restored",
                subject=STATE_KEY,
                mold_id=confirmation.mold_id,
                generation=confirmation.generation,
                expires_at=round(confirmation.expires_at, 3),
                expired=expired,
                remaining_s=round(confirmation.remaining_s(self._clock.now()), 3),
                severity=("warning" if expired else "notice"),
            )
        )
        return True

    def snapshot(self) -> dict:
        with self._lock:
            latest = self._latest
            now = self._clock.now()
        stored = self.stored()
        expired = latest is not None and latest.is_expired(now)
        return {
            "durable": self.is_durable(),
            "latest": latest.as_dict() if latest else None,
            "stored_mold": stored.get("mold_id") if stored else None,
            "max_age_s": self._limits.confirm_max_age_s,
            "expired": expired,
            "remaining_s": round(latest.remaining_s(now), 3) if latest else None,
        }
