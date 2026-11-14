"""Cycle detection and program matching (pure Python, no Home Assistant).

The engine works on the readings of a power sensor (W):

* :class:`CycleDetector` turns the readings into wash cycles. A cycle starts
  when the power rises above ``idle_power``, is confirmed once it reaches
  ``start_power`` (heater or motor) and ends when the power has stayed at or
  below ``idle_power`` for ``end_delay`` seconds. Activity that never reaches
  ``start_power`` (display switched on, door opening after the end, drying
  fan) is discarded.
* Every finished cycle becomes a *profile*: its power resampled in fixed bins.
* :class:`ProgramMatcher` compares the running cycle with the known profiles
  using an incremental, open-ended dynamic time warping (DTW), so programs
  whose phases are slightly shifted in time still match. The best matching
  profile gives the recognised program and the remaining time.
"""
from __future__ import annotations

import csv
import io
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

BIN_SECONDS = 30.0
# Power above which the log scale saturates (heaters are ~2 kW)
MAX_POWER = 2500.0
# Extra cost of stretching/compressing time in the alignment
WARP_PENALTY = 0.05
# Below this confidence the remaining time blends several candidate programs
UNCERTAIN_CONFIDENCE = 0.0


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def feature(power: float) -> float:
    """Log-scaled power in [0, 1]: heater, pumps and idle stay distinguishable."""
    power = min(max(power, 0.0), MAX_POWER)
    return math.log10(1.0 + power) / math.log10(1.0 + MAX_POWER)


def resample(
    samples: Sequence[tuple[float, float]],
    start: float,
    end: float,
    bin_seconds: float = BIN_SECONDS,
) -> list[float]:
    """Average power per bin between ``start`` and ``end``.

    ``samples`` are (timestamp, power) state changes: the power holds its
    value until the next sample (this is how Home Assistant records states).
    """
    bins = max(1, math.ceil((end - start) / bin_seconds))
    energy = [0.0] * bins
    if not samples:
        return energy

    ordered = sorted(samples)
    for index, (t0, power) in enumerate(ordered):
        t1 = ordered[index + 1][0] if index + 1 < len(ordered) else end
        a, b = max(t0, start), min(t1, end)
        while a < b:
            k = min(int((a - start) // bin_seconds), bins - 1)
            edge = min(b, start + (k + 1) * bin_seconds)
            energy[k] += power * (edge - a)
            a = edge
    return [e / bin_seconds for e in energy]


def energy_wh(samples: Sequence[tuple[float, float]], start: float, end: float) -> float:
    """Energy (Wh) between ``start`` and ``end``."""
    return sum(resample(samples, start, end, 60.0)) / 60.0


@dataclass
class Profile:
    """A finished cycle, used to recognise later ones."""

    duration: float  # seconds
    power: list[float]  # average W per bin
    program: str | None = None
    energy: float = 0.0  # Wh
    started: float = 0.0  # timestamp
    bin_seconds: float = BIN_SECONDS

    @property
    def features(self) -> list[float]:
        return [feature(p) for p in self.power]

    def as_dict(self) -> dict:
        return {
            "duration": round(self.duration, 1),
            "power": [round(p, 1) for p in self.power],
            "program": self.program,
            "energy": round(self.energy, 1),
            "started": self.started,
            "bin_seconds": self.bin_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Profile:
        return cls(
            duration=float(data["duration"]),
            power=[float(p) for p in data["power"]],
            program=data.get("program"),
            energy=float(data.get("energy", 0.0)),
            started=float(data.get("started", 0.0)),
            bin_seconds=float(data.get("bin_seconds", BIN_SECONDS)),
        )


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

IDLE = "idle"
PENDING = "pending"  # some activity, not (yet) a real cycle
RUNNING = "running"


@dataclass
class Cycle:
    """A detected cycle."""

    start: float
    end: float | None = None
    samples: list[tuple[float, float]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        end = self.end if self.end is not None else self.samples[-1][0]
        return end - self.start

    def profile(self, program: str | None = None) -> Profile:
        end = self.end if self.end is not None else self.samples[-1][0]
        return Profile(
            duration=end - self.start,
            power=resample(self.samples, self.start, end),
            program=program,
            energy=energy_wh(self.samples, self.start, end),
            started=self.start,
        )


class CycleDetector:
    """State machine turning power readings into cycles.

    Call :meth:`update` for every new reading and :meth:`tick` periodically
    (or when a timer fires): a cycle can end while the sensor does not report
    anything, because Home Assistant only records changes.
    """

    def __init__(
        self,
        idle_power: float = 3.0,
        start_power: float = 150.0,
        end_delay: float = 180.0,
    ) -> None:
        self.idle_power = idle_power
        self.start_power = start_power
        self.end_delay = end_delay
        self.state = IDLE
        self.cycle: Cycle | None = None
        self._low_since: float | None = None
        self._last: tuple[float, float] | None = None

    @property
    def low_since(self) -> float | None:
        return self._low_since

    def update(self, timestamp: float, power: float) -> list[tuple[str, Cycle]]:
        """Process a reading; returns events ("started" / "finished", cycle)."""
        events = self.tick(timestamp)
        self._last = (timestamp, power)

        if power > self.idle_power:
            self._low_since = None
            if self.state == IDLE:
                self.state = PENDING
                self.cycle = Cycle(start=timestamp)
            assert self.cycle is not None
            self.cycle.samples.append((timestamp, power))
            if self.state == PENDING and power >= self.start_power:
                self.state = RUNNING
                events.append(("started", self.cycle))
        elif self.state != IDLE:
            assert self.cycle is not None
            self.cycle.samples.append((timestamp, power))
            if self._low_since is None:
                self._low_since = timestamp
        return events

    def tick(self, now: float) -> list[tuple[str, Cycle]]:
        """End the cycle if the power has been low for ``end_delay``."""
        if (
            self.state == IDLE
            or self._low_since is None
            or now - self._low_since < self.end_delay
        ):
            return []

        cycle = self.cycle
        ended_at = self._low_since
        was_running = self.state == RUNNING
        self.state = IDLE
        self.cycle = None
        self._low_since = None
        if not was_running or cycle is None:
            return []  # short activity that never became a cycle
        cycle.end = ended_at
        cycle.samples = [s for s in cycle.samples if s[0] <= ended_at]
        return [("finished", cycle)]

    def next_deadline(self) -> float | None:
        """When :meth:`tick` should be called to close the cycle."""
        if self.state == IDLE or self._low_since is None:
            return None
        return self._low_since + self.end_delay


def detect_cycles(
    readings: Iterable[tuple[float, float]], detector: CycleDetector | None = None
) -> list[Cycle]:
    """Run a detector over historical readings and return finished cycles."""
    detector = detector or CycleDetector()
    cycles: list[Cycle] = []
    last = None
    for timestamp, power in readings:
        last = timestamp
        cycles.extend(c for kind, c in detector.update(timestamp, power) if kind == "finished")
    if last is not None:
        cycles.extend(
            c for kind, c in detector.tick(last + detector.end_delay) if kind == "finished"
        )
    return cycles


# ---------------------------------------------------------------------------
# Program matching (incremental open-ended DTW)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    """How the running cycle compares with one profile."""

    profile: Profile
    score: float  # mean absolute feature difference along the alignment
    position: int  # matched bin in the profile
    remaining: float  # seconds


class _ProfileAligner:
    """Incremental DTW of a growing cycle against the beginning of a profile."""

    def __init__(self, profile: Profile, band: int, warp_penalty: float) -> None:
        self.profile = profile
        self.reference = profile.features
        self.band = band
        self.warp_penalty = warp_penalty
        self.row: list[float] | None = None  # cumulative cost, one per profile bin
        self.steps: list[int] | None = None  # path length, for normalisation
        self.length = 0

    def push(self, value: float) -> None:
        ref = self.reference
        n = len(ref)
        i = self.length
        # band grows with elapsed time: later phases can drift more
        band = self.band + i // 8
        lo, hi = max(0, i - band), min(n - 1, i + band)
        inf = math.inf
        row = [inf] * n
        steps = [0] * n
        prev, prev_steps = self.row, self.steps
        # moves that stretch or compress time cost a little extra, so the
        # alignment prefers following the profile at its own pace
        penalty = self.warp_penalty
        for j in range(lo, hi + 1):
            cost = abs(value - ref[j])
            if prev is None:
                if j == 0:
                    row[j], steps[j] = cost, 1
                elif row[j - 1] < inf:
                    row[j], steps[j] = row[j - 1] + cost + penalty, steps[j - 1] + 1
                continue
            best, best_steps = inf, 0
            if j > 0 and prev[j - 1] < inf:  # type: ignore[index]
                best, best_steps = prev[j - 1], prev_steps[j - 1]  # type: ignore[index]
            if prev[j] + penalty < best:  # type: ignore[index]
                best, best_steps = prev[j] + penalty, prev_steps[j]  # type: ignore[index]
            if j > 0 and row[j - 1] + penalty < best:
                best, best_steps = row[j - 1] + penalty, steps[j - 1]
            if best < inf:
                row[j], steps[j] = best + cost, best_steps + 1
        self.row, self.steps = row, steps
        self.length += 1

    def match(self) -> Match | None:
        if self.row is None:
            return None
        best_j, best_score = -1, math.inf
        for j, total in enumerate(self.row):
            if total < math.inf:
                score = total / self.steps[j]  # type: ignore[index]
                if score < best_score:
                    best_j, best_score = j, score
        if best_j < 0:
            return None
        bins = len(self.reference)
        remaining = max(0.0, (bins - 1 - best_j) * self.profile.bin_seconds)
        return Match(self.profile, best_score, best_j, remaining)


class ProgramMatcher:
    """Recognise the running program among the known profiles."""

    def __init__(
        self,
        profiles: Sequence[Profile],
        band_bins: int = 6,
        warp_penalty: float = WARP_PENALTY,
    ) -> None:
        self._aligners = [
            _ProfileAligner(p, band_bins, warp_penalty) for p in profiles if p.power
        ]
        self._partial = 0.0
        self._partial_time = 0.0
        self.bins = 0

    def add_bin(self, power: float) -> None:
        """Add the average power of one finished bin of the running cycle."""
        value = feature(power)
        for aligner in self._aligners:
            aligner.push(value)
        self.bins += 1

    def matches(self) -> list[Match]:
        """Matches sorted from best to worst."""
        found = [m for a in self._aligners if (m := a.match()) is not None]
        return sorted(found, key=lambda m: m.score)

    def best(self) -> Match | None:
        found = self.matches()
        return found[0] if found else None


# Above this distance the running cycle does not look like any known run: it
# is a program never seen before (calibrated on real dishwasher logs: runs of
# a known program score below 0.1 from the second minute, a program compared
# only with very different ones 0.25 or more).
NO_MATCH_SCORE = 0.15


def estimate(matches: Sequence[Match]) -> tuple[float | None, str | None, float]:
    """Remaining seconds, program and confidence (0-1) from the matches.

    Nothing is estimated while no known run resembles the cycle (a program
    that has not been learned yet).

    The confidence compares the best match with the best match of a
    different program. When it is high, the remaining time is a weighted
    average (weight 1/score²) of the best runs of the recognised program;
    when it is low, of the best candidates of any program.
    """
    if not matches or matches[0].score > NO_MATCH_SCORE:
        return None, None, 0.0
    best = matches[0]
    other = next(
        (m for m in matches[1:] if _program_key(m) != _program_key(best)), None
    )
    if other is None:
        confidence = 1.0 / (1.0 + best.score * 10)
    else:
        margin = (other.score - best.score) / max(other.score, 1e-6)
        confidence = max(0.0, min(1.0, margin * 2)) / (1.0 + best.score * 5)

    if confidence >= UNCERTAIN_CONFIDENCE:
        # recognised: only runs of that program contribute
        top = [m for m in matches if _program_key(m) == _program_key(best)][:3]
    else:
        # still uncertain (typically at the start, when programs look alike):
        # blend the candidates instead of jumping to one of them
        top = list(matches[:4])
    weights = [1.0 / max(m.score, 0.005) ** 2 for m in top]
    remaining = sum(w * m.remaining for w, m in zip(weights, top, strict=True)) / sum(weights)
    return remaining, best.profile.program, round(confidence, 2)


def _program_key(match: Match) -> object:
    """Unlabelled runs count as programs of their own."""
    return match.profile.program or id(match.profile)


# ---------------------------------------------------------------------------
# Readings exported from Home Assistant
# ---------------------------------------------------------------------------

_TIME_COLUMNS = ("last_changed", "last_updated", "time", "timestamp")


def read_history_csv(text: str) -> list[tuple[float, float]]:
    """Readings of a CSV exported from the Home Assistant history.

    Expected columns: ``entity_id`` (optional), ``state`` and one of
    ``last_changed`` / ``last_updated`` / ``time`` (ISO 8601). Rows that are
    not numbers (``unavailable``, ``unknown``) are skipped. When the file
    holds several entities, the one with most readings is used.
    """
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows:
        return []
    columns = rows[0].keys()
    time_column = next((c for c in _TIME_COLUMNS if c in columns), None)
    if time_column is None or "state" not in columns:
        raise ValueError("missing state / last_changed columns")
    if "entity_id" in columns:
        entity = Counter(row["entity_id"] for row in rows).most_common(1)[0][0]
        rows = [row for row in rows if row["entity_id"] == entity]

    readings: list[tuple[float, float]] = []
    for row in rows:
        try:
            power = float(row["state"])
            moment = datetime.fromisoformat(row[time_column])
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        readings.append((moment.timestamp(), power))
    readings.sort()
    return readings


# ---------------------------------------------------------------------------
# Recognising the program of a finished cycle
# ---------------------------------------------------------------------------

# A finished cycle belongs to the same program as a known run when the whole
# curves match closely and the durations are similar (calibrated on real
# dishwasher logs: runs of the same program score ~0.03, the closest different
# programs ~0.02-0.05 but differ by 10% or more in duration).
SAME_PROGRAM_SCORE = 0.05
SAME_PROGRAM_DURATION = 0.08


def full_match(profile: Profile, reference: Profile) -> Match | None:
    """Align a whole finished cycle with a known run."""
    matcher = ProgramMatcher([reference])
    for power in profile.power:
        matcher.add_bin(power)
    return matcher.best()


def same_program(profile: Profile, reference: Profile) -> bool:
    """True if ``profile`` looks like another run of ``reference``."""
    longest = max(profile.duration, reference.duration, 1.0)
    if abs(profile.duration - reference.duration) / longest > SAME_PROGRAM_DURATION:
        return False
    match = full_match(profile, reference)
    return (
        match is not None
        and match.score < SAME_PROGRAM_SCORE
        and match.remaining <= SAME_PROGRAM_DURATION * reference.duration
    )


def classify(profile: Profile, library: Sequence[Profile]) -> str | None:
    """Program of the closest known run of the same program, if any."""
    best: tuple[float, str] | None = None
    for reference in library:
        if not reference.program or not same_program(profile, reference):
            continue
        match = full_match(profile, reference)
        if match is not None and (best is None or match.score < best[0]):
            best = (match.score, reference.program)
    return best[1] if best else None
