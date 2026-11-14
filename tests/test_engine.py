"""Tests of the cycle engine on real dishwasher logs."""
from __future__ import annotations

import random

import pytest

from custom_components.appliance_cycle.engine import (
    BIN_SECONDS,
    CycleDetector,
    Profile,
    ProgramMatcher,
    classify,
    detect_cycles,
    estimate,
    read_history_csv,
    resample,
    same_program,
)

from .conftest import PROGRAMS, load_trace

# Duration (minutes) of the main cycle in each log. The "auto" log was
# exported while the program was still running, so it has no end.
EXPECTED_MINUTES = {
    "complete": 121.5, "eco_quick": 67.7, "glasses": 79.8,
    "intensive": 126.8, "intensive_extra_rinse": 159.2, "quick": 47.6,
    "quick_shine": 52.4,
}


def _detector() -> CycleDetector:
    return CycleDetector(idle_power=4, start_power=150, end_delay=180)


@pytest.fixture(scope="module")
def cycles():
    found = {}
    for name in PROGRAMS:
        detected = detect_cycles(load_trace(name), _detector())
        if name == "eco":  # this log contains two runs of the same program
            found["eco"], found["eco_2"] = detected
        elif detected:
            found[name] = detected[0]
    return found


def test_one_cycle_per_log(cycles):
    """Display, door opening and drying fan after the end are not cycles."""
    for name in PROGRAMS:
        detected = detect_cycles(load_trace(name), _detector())
        expected = {"eco": 2, "auto": 0}.get(name, 1)
        assert len(detected) == expected, name
    for name, minutes in EXPECTED_MINUTES.items():
        assert cycles[name].duration / 60 == pytest.approx(minutes, abs=0.5), name
    assert cycles["eco"].duration / 60 == pytest.approx(150.6, abs=0.5)
    assert cycles["eco_2"].duration / 60 == pytest.approx(152.4, abs=0.5)


def test_log_cut_while_running():
    detector = _detector()
    for t, p in load_trace("auto"):
        detector.update(t, p)
    assert detector.state == "running"
    assert detector.cycle.duration / 60 == pytest.approx(89.7, abs=0.5)


def test_short_activity_is_not_a_cycle():
    detector = _detector()
    events = []
    for t, p in [(0, 0), (10, 16), (40, 15), (70, 0)]:  # display switched on
        events += detector.update(t, p)
    events += detector.tick(1000)
    assert events == []
    assert detector.state == "idle"


def test_end_waits_for_the_delay():
    detector = _detector()
    started = detector.update(0, 2000)
    assert [kind for kind, _ in started] == ["started"]
    detector.update(600, 2)
    assert detector.tick(700) == []  # pause shorter than the delay
    detector.update(750, 50)
    detector.update(900, 1)
    assert detector.next_deadline() == 1080
    [(kind, cycle)] = detector.tick(1080)
    assert kind == "finished" and cycle.end == 900


def test_resample_holds_values():
    samples = [(0, 100), (45, 200)]
    assert resample(samples, 0, 90) == [100, 150, 200]
    assert sum(resample(samples, 0, 3600, 60)) / 60 == pytest.approx(198.75)


def test_same_program_rules(cycles):
    eco, eco_2 = cycles["eco"].profile("eco"), cycles["eco_2"].profile("eco")
    assert same_program(eco_2, eco)
    # very similar curves but 10% different duration: different programs
    assert not same_program(cycles["quick"].profile(), cycles["quick_shine"].profile())
    profiles = [c.profile(name) for name, c in cycles.items() if name != "eco_2"]
    assert classify(eco_2, profiles) == "eco"
    for name in EXPECTED_MINUTES:
        others = [p for p in profiles if p.program != name]
        assert classify(cycles[name].profile(), others) is None, name


def _perturbed(cycle, rng) -> Profile:
    """Another run of the same program: ±5% duration, ±3% power."""
    stretch = rng.choice([0.95, 1.05])
    samples = sorted(
        (cycle.start + (t - cycle.start) * stretch + rng.uniform(-5, 5), p * rng.uniform(0.97, 1.03))
        for t, p in cycle.samples
    )
    end = cycle.start + (cycle.end - cycle.start) * stretch
    return Profile(duration=end - cycle.start, power=resample(samples, cycle.start, end))


def test_remaining_time_accuracy(cycles):
    """From half of the cycle the program is recognised and the error is small."""
    rng = random.Random(1)
    errors: dict[float, list[float]] = {0.5: [], 0.75: [], 0.9: []}
    for name, cycle in cycles.items():
        program = "eco" if name.startswith("eco") and name != "eco_quick" else name
        library = [c.profile("eco" if n in ("eco", "eco_2") else n) for n, c in cycles.items() if n != name]
        if not any(p.program == program for p in library):
            run = _perturbed(cycle, rng)
            run.program = program
            library.append(run)

        matcher = ProgramMatcher(library)
        bins = resample(cycle.samples, cycle.start, cycle.end)
        checkpoints = {round(len(bins) * f): f for f in errors}
        for index, power in enumerate(bins, start=1):
            matcher.add_bin(power)
            if index in checkpoints:
                remaining, recognised, _ = estimate(matcher.matches())
                true_remaining = cycle.duration - index * BIN_SECONDS
                assert recognised == program, (name, checkpoints[index])
                errors[checkpoints[index]].append(abs(remaining - true_remaining) / 60)

    assert max(errors[0.5]) < 6
    assert max(errors[0.75]) < 4
    assert max(errors[0.9]) < 3
    assert sorted(errors[0.9])[len(errors[0.9]) // 2] < 1.5


def test_profile_roundtrip(cycles):
    profile = cycles["quick"].profile("Quick")
    again = Profile.from_dict(profile.as_dict())
    assert again.program == "Quick"
    assert again.duration == pytest.approx(profile.duration, abs=0.1)
    assert len(again.power) == len(profile.power)


def _run(cycle, library):
    """Feed a cycle bin by bin; yields (elapsed minutes, true remaining, estimate)."""
    matcher = ProgramMatcher(library)
    for i, power in enumerate(resample(cycle.samples, cycle.start, cycle.end)):
        matcher.add_bin(power)
        elapsed = (i + 1) * BIN_SECONDS
        remaining, program, _ = estimate(matcher.matches())
        yield elapsed / 60, (cycle.duration - elapsed) / 60, remaining, program


def test_unknown_program_has_no_estimate(cycles):
    """Only Eco is known: a quick program must not get Eco's remaining time."""
    library = [cycles["eco"].profile("Eco")]
    for minute, _, remaining, program in _run(cycles["eco_quick"], library):
        if minute >= 5:
            assert remaining is None and program is None, minute


def test_second_run_of_a_program(cycles):
    """A later real run of eco quick, recognised from the first one."""
    second = detect_cycles(load_trace("eco_quick_2"), _detector())
    assert len(second) == 1
    library = [cycle.profile(name) for name, cycle in cycles.items()]
    errors, programs = [], []
    for minute, true_remaining, remaining, program in _run(second[0], library):
        if minute >= 5:
            errors.append(abs(remaining / 60 - true_remaining))
            programs.append(program)
    # quick shine looks alike for a moment around minute 16
    assert sum(e < 2 for e in errors) >= 0.95 * len(errors)
    assert programs.count("eco_quick") >= 0.95 * len(programs)
    assert programs[-40:] == ["eco_quick"] * 40


def test_read_history_csv():
    text = (
        "\ufeffentity_id,state,last_changed\n"
        "sensor.plug_power,0,2026-11-14T12:00:00.000Z\n"
        "sensor.plug_power,unavailable,2026-11-14T12:00:10.000Z\n"
        "sensor.other,99,2026-11-14T12:00:15.000Z\n"
        "sensor.plug_power,1900.5,2026-11-14T12:00:20.000Z\n"
        "sensor.plug_power,3,2026-11-14T12:00:30+01:00\n"
    )
    readings = read_history_csv(text)  # sensor.other ignored, sorted by time
    assert [p for _, p in readings] == [3.0, 0.0, 1900.5]
    assert readings[1][0] - readings[0][0] == 3600 - 30
    with pytest.raises(ValueError):
        read_history_csv("a,b\n1,2\n")
