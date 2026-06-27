import logging

import pytest

from solax_ant_controller import controller as main


def snapshot(soc=80.0, battery_power_w=3000.0):
    return main.SolaxSnapshot(
        inverter_sn=None,
        wifi_sn=None,
        upload_time=None,
        utc_datetime=None,
        inverter_status_code=None,
        inverter_status="Unknown",
        battery_status_code=None,
        battery_status="Unknown",
        battery_soc_percent=soc,
        battery_power_w=battery_power_w,
        battery_draining=battery_power_w is not None and battery_power_w < 0,
        ac_output_power_w=None,
        pv_total_power_w=0.0,
        pv1_power_w=None,
        pv2_power_w=None,
        pv3_power_w=None,
        pv4_power_w=None,
        grid_feed_in_power_w=None,
        meter2_power_w=None,
        eps1_power_w=None,
        eps2_power_w=None,
        eps3_power_w=None,
        eps_total_power_w=0.0,
        yield_today_kwh=None,
        yield_total_kwh=None,
        grid_import_energy_kwh=None,
        grid_export_energy_kwh=None,
    )


@pytest.fixture(autouse=True)
def clean_miner_env(monkeypatch):
    for name in (
        "MINER_START_SOC_PERCENT",
        "MINER_FULL_PERCENT",
        "MINER_START_PERCENT",
        "MINER_75_PERCENT",
        "MINER_RAMP_UP_PERCENT_STEP",
        "MINER_BATTERY_CHARGE_RESERVE_W",
    ):
        monkeypatch.delenv(name, raising=False)


def test_decision_pauses_until_battery_reaches_start_soc():
    decision = main.decide_miner_action(
        snapshot=snapshot(soc=74.9, battery_power_w=5000),
        last_target_percent=None,
        miner_power_w=None,
        power_limits=main.MinerPowerLimits(),
    )

    assert decision.action == "pause"
    assert decision.reason == "battery_soc_below_start_threshold_pause_mining"


def test_decision_pauses_when_battery_is_not_charging_enough(monkeypatch):
    monkeypatch.setenv("MINER_BATTERY_CHARGE_RESERVE_W", "100")

    decision = main.decide_miner_action(
        snapshot=snapshot(soc=80, battery_power_w=100),
        last_target_percent=None,
        miner_power_w=None,
        power_limits=main.MinerPowerLimits(),
    )

    assert decision.action == "pause"
    assert decision.reason == "battery_not_charging_enough_pause_mining"


def test_decision_starts_at_miner_minimum_when_limits_are_known():
    limits = main.MinerPowerLimits(
        min_power_w=2414,
        max_power_w=6435,
        rated_power_w=3500,
    )

    decision = main.decide_miner_action(
        snapshot=snapshot(soc=75, battery_power_w=3000),
        last_target_percent=None,
        miner_power_w=None,
        power_limits=limits,
    )

    assert decision.action == "set_percent"
    assert decision.target_percent == pytest.approx(68.971)


def test_decision_ramps_up_by_configured_step(monkeypatch):
    monkeypatch.setenv("MINER_RAMP_UP_PERCENT_STEP", "5")
    limits = main.MinerPowerLimits(
        min_power_w=2414,
        max_power_w=6435,
        rated_power_w=3500,
    )

    decision = main.decide_miner_action(
        snapshot=snapshot(soc=85, battery_power_w=2000),
        last_target_percent=70,
        miner_power_w=2500,
        power_limits=limits,
    )

    assert decision.action == "set_percent"
    assert decision.target_percent == 75


def test_decision_caps_ramp_to_keep_battery_charging():
    limits = main.MinerPowerLimits(
        min_power_w=2414,
        max_power_w=6435,
        rated_power_w=3500,
    )

    decision = main.decide_miner_action(
        snapshot=snapshot(soc=85, battery_power_w=100),
        last_target_percent=70,
        miner_power_w=2500,
        power_limits=limits,
    )

    assert decision.action == "set_percent"
    assert decision.target_percent == pytest.approx(74.286)


def test_decision_pauses_when_surplus_cannot_cover_miner_minimum():
    limits = main.MinerPowerLimits(
        min_power_w=2414,
        max_power_w=6435,
        rated_power_w=3500,
    )

    decision = main.decide_miner_action(
        snapshot=snapshot(soc=80, battery_power_w=100),
        last_target_percent=None,
        miner_power_w=None,
        power_limits=limits,
    )

    assert decision.action == "pause"
    assert decision.reason == "charging_surplus_below_miner_minimum_pause_mining"


class FakeMiner:
    def __init__(self, set_error=None):
        self.calls = []
        self.set_error = set_error

    def pause_mining(self):
        self.calls.append(("pause", None))
        return {"paused": True}

    def stop_mining(self):
        raise AssertionError("stop_mining must not be used for battery control")

    def resume_mining(self):
        self.calls.append(("resume", None))
        return {"resumed": True}

    def start_mining(self):
        self.calls.append(("start", None))
        return {"started": True}

    def set_relative_power_target(self, percentage, reference):
        self.calls.append(("set", percentage, reference))
        if self.set_error is not None:
            error = self.set_error
            self.set_error = None
            raise error
        return {"set": percentage, "reference": reference}


def test_apply_below_braiins_minimum_pauses_and_learns_limits():
    error = RuntimeError(
        "Braiins API error: method=PATCH "
        "path=/api/v1/performance/power-target/relative status=400 "
        'body={"error":"Operation was attempted past the valid range",'
        '"message":"new power target \'1750\' is out-of-range '
        '(min: Some(2414), max: Some(6435))"}'
    )
    miner = FakeMiner(set_error=error)
    limits = main.MinerPowerLimits()

    state, applied, result = main.apply_miner_decision(
        logger=logging.getLogger("test"),
        miner=miner,
        decision=main.MinerDecision(
            action="set_percent",
            target_percent=50,
            reason="test",
        ),
        reference=1,
        last_state=None,
        control_enabled=True,
        power_limits=limits,
    )

    assert state == "pause:0"
    assert applied is True
    assert result["paused"] is True
    assert limits.min_power_w == 2414
    assert limits.max_power_w == 6435
    assert limits.rated_power_w == 3500
    assert miner.calls == [
        ("resume", None),
        ("start", None),
        ("set", 50, 1),
        ("pause", None),
    ]


def test_apply_above_braiins_maximum_clamps_and_retries():
    error = RuntimeError(
        "Braiins API error: method=PATCH "
        "path=/api/v1/performance/power-target/relative status=400 "
        'body={"error":"Operation was attempted past the valid range",'
        '"message":"new power target \'7000\' is out-of-range '
        '(min: Some(2414), max: Some(6435))"}'
    )
    miner = FakeMiner(set_error=error)
    limits = main.MinerPowerLimits()

    state, applied, result = main.apply_miner_decision(
        logger=logging.getLogger("test"),
        miner=miner,
        decision=main.MinerDecision(
            action="set_percent",
            target_percent=200,
            reason="test",
        ),
        reference=1,
        last_state=None,
        control_enabled=True,
        power_limits=limits,
    )

    assert state == "set_percent:183.857"
    assert applied is True
    assert result["clamped"] is True
    assert result["applied_target_percent"] == pytest.approx(183.857)
    assert miner.calls == [
        ("resume", None),
        ("start", None),
        ("set", 200, 1),
        ("set", pytest.approx(183.857), 1),
    ]


def test_apply_skips_repeated_pause_state():
    miner = FakeMiner()

    state, applied, result = main.apply_miner_decision(
        logger=logging.getLogger("test"),
        miner=miner,
        decision=main.MinerDecision(
            action="pause",
            target_percent=0,
            reason="test",
        ),
        reference=1,
        last_state="pause:0",
        control_enabled=True,
        power_limits=main.MinerPowerLimits(),
    )

    assert state == "pause:0"
    assert applied is False
    assert result["skipped"] is True
    assert miner.calls == []
