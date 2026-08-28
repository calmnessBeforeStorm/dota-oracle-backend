"""Phase 7: noticing when the served model stops being calibrated (spec sections 11, 12).

The risk this watches is named in section 12: the meta drifts every two or three months, and
a model degrades quietly. Log loss barely moves at first - what goes first is calibration.
The model still ranks the right side ahead, it just stops being right about *how far* ahead,
and nothing on the site looks broken while it happens.

Three decisions shape what an alert here can honestly mean.

**Drift is a change, not a level.** The obvious design - alert when ECE exceeds a fixed
number - does not work here. The served baseline sits at an ECE around 0.056 permanently;
a threshold above that never fires, and one below it fires forever and gets ignored. What
matters is that a model got worse than it has been, so the recent window is always compared
against the same model's own earlier record.

**The threshold is derived from the window, not chosen.** ECE is an estimate, and a small
sample moves it a long way on its own. Measured on our own log by repeatedly splitting one
model's matches into random halves and comparing the two ECEs:

    85 matches per half   median gap 0.034   p90 0.077
    50 matches per half   median gap 0.038   p90 0.098
    25 matches per half   median gap 0.058   p90 0.135

So an alert threshold that is right today is wrong after a month of data: the noise floor
falls as the windows grow, and a fixed 0.08 would go deaf. `noise_floor` reproduces that
curve from the smaller of the two windows, so the alarm tightens as the evidence improves.

**Matches, never rows.** Every count here is in matches. Forty snapshots of one game are
forty views of one outcome (section 5.1), and a window measured in rows would call sixty
correlated rows a large sample.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.api.accuracy import ScoredPrediction
from app.ml.metrics import expected_calibration_error

#: How far back "recent" reaches. Long enough to hold a few tournament days - a shorter
#: window mostly measures which teams happened to play.
RECENT_DAYS = 7

#: Below this a window is not evidence. Measured: the same model scored 0.070 log loss in the
#: 30+ bucket on 14 matches and 2.356 on 171, so a verdict from a handful of games says more
#: about which games than about the model.
MIN_MATCHES = 40

#: The p90 gap between two halves of one model's own record, at 85 matches a side. The floor
#: is scaled from here by sample size - see `noise_floor`.
NOISE_AT_85 = 0.077

#: Even a well-evidenced change this small is not worth waking anybody for.
MIN_MEANINGFUL_RISE = 0.02


@dataclass(frozen=True)
class Window:
    """One slice of a model's scored predictions."""

    label: str
    ece: float
    matches: int
    rows: int
    first: datetime
    last: datetime


@dataclass(frozen=True)
class DriftVerdict:
    model_version: str
    recent: Window | None
    reference: Window | None
    threshold: float | None
    verdict: str
    detail: str

    @property
    def is_alerting(self) -> bool:
        return self.verdict == "drifting"

    def as_log_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "model_version": self.model_version,
            "verdict": self.verdict,
            "detail": self.detail,
        }
        if self.recent:
            fields |= {
                "recent_ece": round(self.recent.ece, 4),
                "recent_matches": self.recent.matches,
            }
        if self.reference:
            fields |= {
                "reference_ece": round(self.reference.ece, 4),
                "reference_matches": self.reference.matches,
            }
        if self.threshold is not None:
            fields["threshold"] = round(self.threshold, 4)
        return fields


def noise_floor(matches: int) -> float:
    """How far ECE moves on this many matches for no reason at all.

    Scaled as one over the square root of the sample, which is how the standard error of a
    proportion behaves and which fits the three measured points closely: 0.077 at 85 matches
    predicts 0.100 at 50 and 0.142 at 25, against 0.098 and 0.135 observed.

    This is what keeps the alert alive as the dataset grows. A constant chosen today would
    go deaf: at ten thousand matches a real drift of 0.03 would sit far under a floor set for
    eighty-five.
    """
    if matches < 1:
        raise ValueError("a window needs at least one match")
    return NOISE_AT_85 * math.sqrt(85.0 / matches)


def _window(label: str, rows: Sequence[ScoredPrediction]) -> Window:
    return Window(
        label=label,
        ece=expected_calibration_error([r.radiant_win for r in rows], [r.p_radiant for r in rows]),
        matches=len({r.match_id for r in rows}),
        rows=len(rows),
        first=min(r.predicted_at for r in rows),
        last=max(r.predicted_at for r in rows),
    )


def check_drift(
    version: str,
    scored: Sequence[ScoredPrediction],
    now: datetime,
    recent_days: int = RECENT_DAYS,
) -> DriftVerdict:
    """Compare the last `recent_days` of one model's predictions against everything before.

    Both halves come from the same model on purpose. Comparing against a different version
    would measure the change of model rather than the drift of this one, and comparing
    against a fixed number would measure the model's permanent level.
    """
    cutoff = now - timedelta(days=recent_days)
    recent_rows = [row for row in scored if row.predicted_at >= cutoff]
    earlier_rows = [row for row in scored if row.predicted_at < cutoff]

    def not_enough(reason: str) -> DriftVerdict:
        return DriftVerdict(
            model_version=version,
            recent=_window("recent", recent_rows) if recent_rows else None,
            reference=_window("reference", earlier_rows) if earlier_rows else None,
            threshold=None,
            verdict="not enough data",
            detail=reason,
        )

    if not recent_rows or not earlier_rows:
        return not_enough("one of the two windows is empty")

    recent = _window("recent", recent_rows)
    reference = _window("reference", earlier_rows)
    if min(recent.matches, reference.matches) < MIN_MATCHES:
        return not_enough(
            f"{min(recent.matches, reference.matches)} matches in the smaller window, "
            f"{MIN_MATCHES} needed"
        )

    # The smaller window sets the floor: a precise reference tells you nothing if the recent
    # side is three games.
    threshold = max(noise_floor(min(recent.matches, reference.matches)), MIN_MEANINGFUL_RISE)
    rise = recent.ece - reference.ece

    if rise > threshold:
        verdict, detail = (
            "drifting",
            f"calibration error rose {rise:.4f} against a {threshold:.4f} floor",
        )
    elif rise < -threshold:
        verdict, detail = ("improving", f"calibration error fell {abs(rise):.4f}")
    else:
        verdict, detail = ("ok", f"change of {rise:+.4f} is inside the {threshold:.4f} floor")

    return DriftVerdict(
        model_version=version,
        recent=recent,
        reference=reference,
        threshold=threshold,
        verdict=verdict,
        detail=detail,
    )
