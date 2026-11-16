"""PHQ-9 and GAD-7: standard item text, scoring and neutral severity labels."""
from __future__ import annotations

from dataclasses import dataclass

CHOICES: tuple[tuple[int, str], ...] = (
    (0, "Not at all"),
    (1, "Several days"),
    (2, "More than half the days"),
    (3, "Nearly every day"),
)


@dataclass(frozen=True)
class Instrument:
    key: str
    title: str
    prompt: str
    items: tuple[str, ...]
    bands: tuple[tuple[int, str], ...]  # (inclusive upper bound, label)

    @property
    def max_score(self) -> int:
        return 3 * len(self.items)

    def band(self, total: int) -> str:
        for upper, label in self.bands:
            if total <= upper:
                return label
        return self.bands[-1][1]


PHQ9 = Instrument(
    key="phq9",
    title="PHQ-9",
    prompt="Over the last 2 weeks, how often have you been bothered by the following?",
    items=(
        "Little interest or pleasure in doing things",
        "Feeling down, depressed, or hopeless",
        "Trouble falling or staying asleep, or sleeping too much",
        "Feeling tired or having little energy",
        "Poor appetite or overeating",
        "Feeling bad about yourself, or that you are a failure or have let yourself or your family down",  # noqa: E501
        "Trouble concentrating on things, such as reading the newspaper or watching television",
        "Moving or speaking so slowly that other people could have noticed, or the opposite: being so fidgety or restless that you have been moving around a lot more than usual",  # noqa: E501
        "Thoughts that you would be better off dead, or of hurting yourself in some way",
    ),
    bands=((4, "minimal"), (9, "mild"), (14, "moderate"), (19, "moderately severe"), (27, "severe")),  # noqa: E501
)

GAD7 = Instrument(
    key="gad7",
    title="GAD-7",
    prompt="Over the last 2 weeks, how often have you been bothered by the following problems?",
    items=(
        "Feeling nervous, anxious or on edge",
        "Not being able to stop or control worrying",
        "Worrying too much about different things",
        "Trouble relaxing",
        "Being so restless that it is hard to sit still",
        "Becoming easily annoyed or irritable",
        "Feeling afraid as if something awful might happen",
    ),
    bands=((4, "minimal"), (9, "mild"), (14, "moderate"), (21, "severe")),
)

INSTRUMENTS: dict[str, Instrument] = {PHQ9.key: PHQ9, GAD7.key: GAD7}

MAX_COMMENT_CHARS = 4000


def validate_answers(instrument: Instrument, answers: list[int]) -> list[int]:
    if len(answers) != len(instrument.items):
        raise ValueError(f"{instrument.title} needs {len(instrument.items)} answers")
    if any(isinstance(a, bool) or not isinstance(a, int) or not 0 <= a <= 3 for a in answers):
        raise ValueError("each answer must be an integer from 0 to 3")
    return list(answers)


def score(answers: list[int]) -> int:
    """Plain sum. Every item, including item 9, counts like any other."""
    return sum(answers)


def describe() -> list[dict]:
    return [
        {
            "key": i.key,
            "title": i.title,
            "prompt": i.prompt,
            "items": list(i.items),
            "choices": [{"value": v, "label": text} for v, text in CHOICES],
            "max_score": i.max_score,
        }
        for i in INSTRUMENTS.values()
    ]
