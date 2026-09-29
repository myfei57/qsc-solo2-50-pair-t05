"""模温确认与硫化计时：落盘、闩锁与进度判定。"""

from __future__ import annotations

import pytest

from rubbercure.errors import InterlockError
from rubbercure.temp.read import MoldTempReading
from rubbercure.temp.target import TempTarget
from rubbercure.temp.tolerance import band_summary, spread_ok, within_band
from rubbercure.timer.done import complete_cure
from rubbercure.timer.latch import CureLatch
from rubbercure.timer.validate import validate_progress


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
    assert reloaded.restore() is True
    assert reloaded.latest().mold_id == "mold-T3"


def test_confirmation_carries_expiry_and_is_durable_until_then(platform, clock):
    unit = platform.press("press-01")
    max_age = platform.config.temp.confirm_max_age_s
    reading = MoldTempReading(
        mold_id="mold-E",
        generation=1,
        sampled_at=clock.now(),
        by_cavity={0: platform.config.temp.setpoint_c},
    )
    confirmation = unit.temp_persister.confirm(reading, platform.config.temp.confirm_hold_s)
    assert confirmation.expires_at == round(clock.now() + max_age, 4)
    assert unit.temp_persister.stored()["expires_at"] == confirmation.expires_at
    assert not confirmation.is_expired(clock.now())

    # 恰好到过期时刻仍算数，越过之后必须重新确认。
    clock.advance(max_age)
    assert unit.temp_persister.is_durable()
    clock.advance(0.001)
    assert not unit.temp_persister.is_durable()
    snapshot = unit.temp_persister.snapshot()
    assert snapshot["expired"] is True
    assert snapshot["remaining_s"] < 0
    with pytest.raises(InterlockError, match="超期"):
        unit.temp_persister.require_durable()

    # 超期不删落盘文档：保留审计痕迹，只是判定不再算数。
    assert unit.temp_persister.stored() is not None

    # 重新确认后恢复有效，新的过期时刻顺延。
    clock.advance(10.0)
    renewed = unit.temp_persister.confirm(reading, platform.config.temp.confirm_hold_s)
    assert renewed.expires_at == round(clock.now() + max_age, 4)
    assert unit.temp_persister.is_durable()


def test_restored_confirmation_is_judged_by_persisted_expiry(platform, clock):
    unit = platform.press("press-02")
    reading = MoldTempReading(
        mold_id="mold-R1",
        generation=1,
        sampled_at=clock.now(),
        by_cavity={0: platform.config.temp.setpoint_c},
    )
    unit.temp_persister.confirm(reading, platform.config.temp.confirm_hold_s)
    clock.advance(100.0)

    # 重启恢复：仍在有效期内，按下盘那份继续算数。
    fresh = type(unit.temp_persister)(clock, platform.bus, unit.store, platform.config.temp)
    assert fresh.restore() is True
    assert fresh.is_durable()

    # 时间越过有效期后，即使没有新进程，同一份恢复记录也不再算数。
    clock.advance(platform.config.temp.confirm_max_age_s)
    assert not fresh.is_durable()

    # 直接以"已超期"状态重启：恢复得到记录，但联锁必须拒绝。
    expired = type(unit.temp_persister)(clock, platform.bus, unit.store, platform.config.temp)
    assert expired.restore() is True
    assert expired.snapshot()["expired"] is True
    with pytest.raises(InterlockError, match="超期"):
        expired.require_durable()


def test_restored_legacy_document_without_expiry_is_discarded(platform):
    unit = platform.press("press-02")
    unit.store.write(
        "temp_confirmation",
        {
            "mold_id": "legacy",
            "generation": 1,
            "setpoint_c": platform.config.temp.setpoint_c,
            "confirmed_c": platform.config.temp.setpoint_c,
            "confirmed_at": 100.0,
            "hold_s": 60.0,
        },
    )
    persister = type(unit.temp_persister)(
        platform.clock, platform.bus, unit.store, platform.config.temp
    )
    assert persister.restore() is False
    assert persister.latest() is None
    # 无法证明仍在有效期内的历史文档直接删除，强制重新确认。
    assert persister.stored() is None
    actions = [event.action for event in platform.bus.history("temp")]
    assert "temp_confirmation_discarded" in actions


def test_loading_another_mold_invalidates_confirmation(platform, clock):
    unit = platform.press("press-03")
    reading = MoldTempReading(
        mold_id="mold-S1",
        generation=1,
        sampled_at=clock.now(),
        by_cavity={0: platform.config.temp.setpoint_c},
    )
    unit.temp_persister.confirm(reading, platform.config.temp.confirm_hold_s)

    # 换上不同模具：内存与落盘确认同时作废。
    unit.load_mold("mold-S2")
    assert not unit.temp_persister.is_durable()
    assert unit.temp_persister.latest() is None
    assert unit.temp_persister.stored() is None
    actions = [event.action for event in platform.bus.history("temp")]
    assert "temp_confirmation_invalidated" in actions

    # 装回同一副模具（含重启后恢复的场景）不影响仍在有效期内的确认。
    same = MoldTempReading(
        mold_id="mold-S3",
        generation=1,
        sampled_at=clock.now(),
        by_cavity={0: platform.config.temp.setpoint_c},
    )
    unit.temp_persister.confirm(same, platform.config.temp.confirm_hold_s)
    restarted = type(unit)(
        "press-03", clock, platform.bus, unit.store, platform.config
    )
    restarted.temp_persister.restore()
    restarted.load_mold("mold-S3")
    assert restarted.temp_persister.is_durable()


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
