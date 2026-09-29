"""模温确认与硫化计时：落盘、有效期、闩锁与进度判定。"""

from __future__ import annotations

import pytest

from rubbercure.errors import InterlockError
from rubbercure.temp.read import MoldTempReading
from rubbercure.temp.target import TempTarget
from rubbercure.temp.tolerance import band_summary, spread_ok, within_band
from rubbercure.timer.done import complete_cure
from rubbercure.timer.latch import CureLatch
from rubbercure.timer.validate import validate_progress


def _reading(platform, mold_id: str, generation: int = 1) -> MoldTempReading:
    setpoint = platform.config.temp.setpoint_c
    return MoldTempReading(
        mold_id=mold_id,
        generation=generation,
        sampled_at=platform.clock.now(),
        by_cavity={0: setpoint, 1: setpoint + 0.3},
    )


def test_tolerance_helpers_agree_with_target_band(platform):
    limits = platform.config.temp
    assert within_band(limits.setpoint_c, limits.setpoint_c, limits.tolerance_c)
    assert not within_band(limits.setpoint_c + limits.tolerance_c + 0.01, limits.setpoint_c, limits.tolerance_c)
    assert spread_ok(1.2, limits.max_cavity_spread_c)
    assert not spread_ok(9.9, limits.max_cavity_spread_c)
    summary = band_summary(limits.setpoint_c - 1.0, limits.setpoint_c, limits.tolerance_c)
    assert summary["in_band"] is True
    target = TempTarget(limits.setpoint_c, limits.tolerance_c, limits.warmup_rate_c_per_min)
    assert target.is_in_band(limits.setpoint_c)
    assert target.clamp(limits.setpoint_c + 99.0) == target.band()[1]


def test_confirm_rejects_temperature_outside_band(platform):
    unit = platform.press("press-01")
    reading = MoldTempReading(
        mold_id="mold-T",
        generation=1,
        sampled_at=platform.clock.now(),
        by_cavity={0: 100.0, 1: 101.0},
    )
    with pytest.raises(InterlockError):
        unit.temp_persister.confirm(reading, platform.config.temp.confirm_hold_s)


def test_confirm_rejects_short_hold(platform):
    unit = platform.press("press-01")
    reading = MoldTempReading(
        mold_id="mold-T2",
        generation=1,
        sampled_at=platform.clock.now(),
        by_cavity={0: platform.config.temp.setpoint_c},
    )
    with pytest.raises(InterlockError):
        unit.temp_persister.confirm(reading, platform.config.temp.confirm_hold_s - 1.0)


def test_confirmation_is_durable_and_restorable(platform):
    unit = platform.press("press-02")
    reading = MoldTempReading(
        mold_id="mold-T3",
        generation=2,
        sampled_at=platform.clock.now(),
        by_cavity={0: platform.config.temp.setpoint_c, 1: platform.config.temp.setpoint_c + 0.5},
    )
    confirmation = unit.temp_persister.confirm(reading, platform.config.temp.confirm_hold_s)
    assert unit.temp_persister.is_durable()
    assert unit.temp_persister.stored()["mold_id"] == "mold-T3"
    assert unit.temp_persister.snapshot()["stored_mold"] == "mold-T3"
    assert unit.temp_persister.require_durable().digest == confirmation.digest

    reloaded = type(unit.temp_persister)(
        platform.clock, platform.bus, unit.store, platform.config.temp
    )
    result = reloaded.restore()
    assert result == {"restored": True, "valid": True}
    assert reloaded.latest().mold_id == "mold-T3"


def test_confirmation_expires_after_ttl(platform):
    unit = platform.press("press-01")
    ttl_s = platform.config.temp.confirm_ttl_s
    unit.temp_persister.confirm(_reading(platform, "mold-E1"), platform.config.temp.confirm_hold_s)
    assert unit.temp_persister.is_durable()

    # 临界时刻仍算数：有效期是闭区间
    platform.clock.advance(ttl_s)
    assert unit.temp_persister.is_durable()
    assert unit.temp_persister.snapshot()["remaining_s"] == 0.0

    platform.clock.advance(0.001)
    assert not unit.temp_persister.is_durable()
    with pytest.raises(InterlockError, match="超过有效期"):
        unit.temp_persister.require_durable("mold-E1", 1)


def test_require_durable_rejects_other_mold_and_older_generation(platform):
    unit = platform.press("press-02")
    unit.temp_persister.confirm(_reading(platform, "mold-G1", generation=3),
                                platform.config.temp.confirm_hold_s)
    with pytest.raises(InterlockError, match="不一致"):
        unit.temp_persister.require_durable("mold-G2", 3)
    with pytest.raises(InterlockError, match="第 4 代"):
        unit.temp_persister.require_durable("mold-G1", 4)
    # 模具与代际都对得上时正常放行
    assert unit.temp_persister.require_durable("mold-G1", 3).generation == 3


def test_loading_a_different_mold_invalidates_confirmation(platform):
    unit = platform.press("press-01")
    unit.load_mold("mold-S1")
    unit.temp_persister.confirm(_reading(platform, "mold-S1"),
                                platform.config.temp.confirm_hold_s)
    assert unit.temp_persister.is_durable()

    unit.load_mold("mold-S2")
    assert not unit.temp_persister.is_durable()
    assert unit.temp_persister.stored() is None
    with pytest.raises(InterlockError, match="尚未落盘"):
        unit.temp_persister.require_durable("mold-S2", 1)

    # 同一台模具重复装模不作废仍有效的确认
    unit.temp_persister.confirm(_reading(platform, "mold-S2"),
                                platform.config.temp.confirm_hold_s)
    unit.load_mold("mold-S2")
    assert unit.temp_persister.is_durable()


def test_restart_judges_persisted_confirmation_by_ttl(platform, tmp_path):
    unit = platform.press("press-03")
    ttl_s = platform.config.temp.confirm_ttl_s
    unit.temp_persister.confirm(_reading(platform, "mold-R1"),
                                platform.config.temp.confirm_hold_s)

    # 机台重启：全新的 persister 从落盘恢复
    platform.clock.advance(ttl_s + 1.0)
    reloaded = type(unit.temp_persister)(
        platform.clock, platform.bus, unit.store, platform.config.temp
    )
    result = reloaded.restore()
    assert result["restored"] is True
    assert result["valid"] is False
    assert not reloaded.is_durable()
    with pytest.raises(InterlockError, match="超过有效期"):
        reloaded.require_durable("mold-R1", 1)

    # 未超期重启：按下盘那份判定仍然算数，模具/代际也要对得上
    fresh_store_root = tmp_path / "fresh"
    from rubbercure.store.jsonfile import JsonStore

    fresh_store = JsonStore(str(fresh_store_root))
    platform.clock.advance(1.0)  # 已过期，先写一份新的
    fresh = type(unit.temp_persister)(
        platform.clock, platform.bus, fresh_store, platform.config.temp
    )
    fresh.confirm(_reading(platform, "mold-R2", generation=5),
                  platform.config.temp.confirm_hold_s)
    platform.clock.advance(ttl_s - 10.0)
    rebooted = type(unit.temp_persister)(
        platform.clock, platform.bus, fresh_store, platform.config.temp
    )
    assert rebooted.restore()["valid"] is True
    assert rebooted.require_durable("mold-R2", 5).mold_id == "mold-R2"
    with pytest.raises(InterlockError):
        rebooted.require_durable("mold-R2", 6)


def test_legacy_confirmation_without_ttl_is_stale_after_restart(platform):
    unit = platform.press("press-02")
    unit.temp_persister.confirm(_reading(platform, "mold-OLD"),
                                platform.config.temp.confirm_hold_s)
    stored = unit.temp_persister.stored()
    del stored["ttl_s"]
    unit.store.write("temp_confirmation", stored)

    reloaded = type(unit.temp_persister)(
        platform.clock, platform.bus, unit.store, platform.config.temp
    )
    assert reloaded.restore()["valid"] is False
    assert not reloaded.is_durable()


def test_latch_is_per_mold(platform):
    unit = platform.press("press-01")
    unit.timer.begin("mold-L1", "RB-215", 600.0, 1)
    unit.timer.accumulate(600.0)
    result = complete_cure(unit.timer, platform.latch, platform.bus)
    assert result["latch_set"] is True
    assert platform.latch.is_set("mold-L1")
    assert not platform.latch.is_set("mold-L2")

    other = platform.press("press-02")
    other.timer.begin("mold-L2", "RB-215", 600.0, 1)
    other.timer.accumulate(600.0)
    complete_cure(other.timer, platform.latch, platform.bus)
    assert platform.latch.set_molds() == ["mold-L1", "mold-L2"]
    assert platform.latch.reset("mold-L1", platform.clock.now()).released is True
    assert platform.latch.set_molds() == ["mold-L2"]


def test_validate_progress_verdicts(platform):
    limits = platform.config.timer
    assert validate_progress(600.0, 600.0, limits)["verdict"] == "ok"
    assert validate_progress(300.0, 600.0, limits)["verdict"] == "short"
    assert validate_progress(900.0, 600.0, limits)["verdict"] == "over"


def test_latch_release_all_reports_count():
    latch = CureLatch()
    latch.set("a", 1.0)
    latch.set("b", 2.0)
    assert latch.release_all() == 2
    assert latch.set_molds() == []
