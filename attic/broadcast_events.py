"""Extract structured baseball events from broadcast transcript text.

Sits between the audio_transcribe module and the pre-positioning strategy.
Takes raw transcribed text from a radio broadcast and identifies game
events: runs scoring, home runs, strikeouts, pitching changes, etc.

Design principles
-----------------
1. **Pattern-based first, LLM fallback.** Regex patterns catch the
   obvious cases (broadcasters use formulaic language). An optional
   LLM pass handles ambiguous or unusual phrasings. The patterns are
   fast and don't need a network call.

2. **Scoring events are the priority.** For the pre-positioning strategy,
   we care most about runs scoring — that's what moves the book. Other
   events (strikeouts, walks) are tracked for context but aren't
   actionable in the same way.

3. **Reusable beyond baseball.** The event extraction framework
   (pattern registry + confidence scoring + event dedup) works for
   any domain. The baseball patterns are one instantiation.

Usage::

    from broadcast_events import extract_events, EventType

    text = "And that ball is GONE! A two-run homer by Judge!"
    events = extract_events(text)
    for e in events:
        print(f"{e.event_type.name}: {e.description} (conf={e.confidence:.2f})")
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum, auto

log = logging.getLogger("broadcast_events")


# ─── Event types ─────────────────────────────────────────────────────────

class EventType(Enum):
    HOME_RUN = auto()
    RUN_SCORED = auto()       # generic run (sac fly, single scores runner, etc.)
    HIT = auto()              # single, double, triple (non-HR)
    WALK = auto()
    STRIKEOUT = auto()
    DOUBLE_PLAY = auto()
    ERROR = auto()
    STOLEN_BASE = auto()
    PITCHING_CHANGE = auto()
    HIT_BY_PITCH = auto()
    WILD_PITCH = auto()
    INNING_CHANGE = auto()
    GAME_OVER = auto()
    UNKNOWN = auto()


# Scoring events — the ones that move the book
SCORING_EVENTS = {
    EventType.HOME_RUN, EventType.RUN_SCORED,
}

# High-leverage events — may not score but change game state significantly
HIGH_LEVERAGE_EVENTS = {
    EventType.HOME_RUN, EventType.RUN_SCORED, EventType.DOUBLE_PLAY,
    EventType.ERROR, EventType.PITCHING_CHANGE,
}


@dataclass
class BroadcastEvent:
    """One detected event from a broadcast transcript."""
    event_type: EventType
    description: str           # the matched text
    confidence: float          # 0-1, higher = more certain
    timestamp: float           # seconds since stream started
    player_name: str | None = None
    runs_on_play: int = 0     # estimated runs scored on this play
    is_scoring: bool = False
    raw_text: str = ""        # full chunk text for context


# ─── Pattern registry ────────────────────────────────────────────────────

@dataclass
class _Pattern:
    event_type: EventType
    regex: re.Pattern
    confidence: float = 0.9
    runs: int = 0
    scoring: bool = False


# Patterns are checked in order. First match wins per event type.
# Compiled with IGNORECASE for broadcast variation.
_PATTERNS: list[_Pattern] = [
    # ── Home runs (always scoring) ───────────────────────────
    _Pattern(
        EventType.HOME_RUN,
        re.compile(
            r"(?:grand\s+slam|"
            r"(?:home\s*run|homer|homers|goes?\s+yard|"
            r"gone|out\s+of\s+here|see\s+you?\s+later|"
            r"goodbye|good\s*bye|back.{0,10}back.{0,10}back.{0,10}gone|"
            r"deep.{0,15}(?:gone|out\s+of)|"
            r"(?:one|two|three|2|3)\s*(?:-|\s)run\s+(?:home\s*run|homer|shot|blast|bomb|jack)|"
            r"solo\s+(?:home\s*run|homer|shot|blast)|"
            r"(?:hits|hit|smash(?:es)?|crush(?:es)?|launch(?:es)?|blast(?:s)?|"
            r"drill(?:s)?|hammer(?:s)?|rip(?:s)?|jack(?:s)?)\s+"
            r"(?:it\s+)?(?:out\s+of|over\s+the|into\s+the\s+(?:stands|seats|bleachers|bullpen))))",
            re.IGNORECASE,
        ),
        confidence=0.95,
        runs=1,  # minimum; grand slam = 4
        scoring=True,
    ),

    # ── Runs scoring (generic) ───────────────────────────────
    _Pattern(
        EventType.RUN_SCORED,
        re.compile(
            r"(?:(?:scores?|comes?\s+(?:home|around|in)|"
            r"(?:crosses|cross|touching|touch(?:es)?)\s+(?:the\s+)?(?:plate|home)|"
            r"(?:will|he'll|she'll)\s+score|scores?\s+(?:from|on)|"
            r"(?:run|runs)\s+(?:scores?|comes?\s+in)|"
            r"(?:drives?\s+(?:in|home)|plates?|brings?\s+(?:in|home)|"
            r"knocks?\s+in|pushes?\s+across))\s)",
            re.IGNORECASE,
        ),
        confidence=0.85,
        runs=1,
        scoring=True,
    ),

    # ── Hits ─────────────────────────────────────────────────
    _Pattern(
        EventType.HIT,
        re.compile(
            r"(?:(?:singles?|doubles?|triples?)\s+(?:to|into|down|through|off|up)|"
            r"(?:line\s+drive|ground\s+ball|fly\s+ball|sharp\s+grounder|"
            r"(?:rbi|r\.b\.i\.?)\s+(?:single|double|triple))|"
            r"(?:base\s+hit|(?:a|the)\s+hit\s+(?:to|into|down)))",
            re.IGNORECASE,
        ),
        confidence=0.80,
    ),

    # ── Walks ────────────────────────────────────────────────
    _Pattern(
        EventType.WALK,
        re.compile(
            r"(?:(?:walks?|walked|walk(?:s|ed)?\s+(?:him|her))|"
            r"(?:base\s+on\s+balls|four\s+(?:straight\s+)?balls|"
            r"(?:takes?\s+(?:ball\s+four|a\s+walk))|"
            r"(?:free\s+pass|issued?\s+a\s+walk)))",
            re.IGNORECASE,
        ),
        confidence=0.85,
    ),

    # ── Strikeouts ───────────────────────────────────────────
    _Pattern(
        EventType.STRIKEOUT,
        re.compile(
            r"(?:(?:strike(?:s)?\s+(?:him\s+)?out|struck\s+(?:him\s+)?out|"
            r"(?:strikes?\s+out|strikeout|punch(?:es)?\s+(?:him\s+)?out)|"
            r"(?:goes?\s+down\s+(?:on\s+strikes|swinging|looking))|"
            r"(?:called\s+(?:out\s+on\s+)?strike(?:s)?\s*(?:three)?|"
            r"caught\s+(?:him\s+)?looking|swinging\s+strike\s+three|"
            r"(?:^|\s)K(?:\.|\!|\s+(?:looking|swinging))|ring(?:s)?\s+(?:him|her)\s+up)))",
            re.IGNORECASE,
        ),
        confidence=0.85,
    ),

    # ── Double plays ─────────────────────────────────────────
    _Pattern(
        EventType.DOUBLE_PLAY,
        re.compile(
            r"(?:double\s+play|(?:6|4)\s*-?\s*(?:4|6|3)\s*-?\s*3|"
            r"(?:turns?\s+two|turn(?:s|ed)?\s+(?:the|a)\s+double\s+play)|"
            r"(?:twin\s+killing|(?:gets?\s+)?two\s+(?:for\s+)?(?:the\s+)?(?:price\s+of\s+)?one))",
            re.IGNORECASE,
        ),
        confidence=0.85,
    ),

    # ── Errors ───────────────────────────────────────────────
    _Pattern(
        EventType.ERROR,
        re.compile(
            r"(?:(?:error|errors)\s+(?:by|on|at)|"
            r"(?:boots?\s+it|bobbles?\s+it|muffs?\s+it|"
            r"(?:throwing|fielding|dropped)\s+error|"
            r"misplays?\s+(?:the|it|that)))",
            re.IGNORECASE,
        ),
        confidence=0.80,
    ),

    # ── Pitching changes ─────────────────────────────────────
    _Pattern(
        EventType.PITCHING_CHANGE,
        re.compile(
            r"(?:(?:pitching\s+change|new\s+pitcher|"
            r"(?:brings?\s+in|comes?\s+in\s+to\s+pitch|"
            r"(?:warming|warmed)\s+up\s+in\s+the\s+(?:bull\s*pen|pen)|"
            r"(?:out\s+of|from)\s+the\s+(?:bull\s*pen|pen))|"
            r"(?:replaces?|replaced\s+by)|"
            r"(?:(?:goes?|going)\s+to\s+(?:the\s+)?(?:bull\s*pen|pen|mound))))",
            re.IGNORECASE,
        ),
        confidence=0.80,
    ),

    # ── Hit by pitch ─────────────────────────────────────────
    _Pattern(
        EventType.HIT_BY_PITCH,
        re.compile(
            r"(?:hit\s+by\s+(?:a\s+)?pitch|"
            r"(?:plunked|beaned|drilled|nailed)\s|"
            r"(?:takes?\s+(?:one|it)\s+(?:off|in)\s+the))",
            re.IGNORECASE,
        ),
        confidence=0.80,
    ),

    # ── Wild pitch / passed ball ─────────────────────────────
    _Pattern(
        EventType.WILD_PITCH,
        re.compile(
            r"(?:wild\s+pitch|passed\s+ball|"
            r"(?:gets?\s+(?:away|by|past)\s+(?:the\s+)?(?:catcher|backstop)))",
            re.IGNORECASE,
        ),
        confidence=0.75,
        runs=0,
        scoring=False,  # may or may not score a runner
    ),

    # ── Stolen base ──────────────────────────────────────────
    _Pattern(
        EventType.STOLEN_BASE,
        re.compile(
            r"(?:steals?\s+(?:second|third|home)|stolen\s+base|"
            r"(?:takes?\s+off|breaks?\s+for)\s+(?:second|third|home))",
            re.IGNORECASE,
        ),
        confidence=0.80,
    ),

    # ── Inning changes ───────────────────────────────────────
    _Pattern(
        EventType.INNING_CHANGE,
        re.compile(
            r"(?:(?:end|top|bottom|middle)\s+(?:of\s+)?(?:the\s+)?"
            r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|"
            r"1st|2nd|3rd|4th|5th|6th|7th|8th|9th|10th|11th|12th|"
            r"tenth|eleventh|twelfth)(?:\s+inning)?|"
            r"(?:three\s+(?:up|down)|(?:side|inning)\s+(?:is\s+)?retired|"
            r"(?:that'll?\s+)?(?:end|retire|do\s+it\s+for)\s+(?:the\s+)?(?:inning|side)))",
            re.IGNORECASE,
        ),
        confidence=0.70,
    ),

    # ── Game over ────────────────────────────────────────────
    _Pattern(
        EventType.GAME_OVER,
        re.compile(
            r"(?:(?:ball\s*game|game)\s+(?:is\s+)?over|"
            r"(?:that's?\s+(?:the\s+)?(?:ball\s*)?game|final\s+(?:score|out))|"
            r"(?:wins?\s+it|walk\s*off|(?:and\s+)?(?:that'll?\s+)?do\s+it))",
            re.IGNORECASE,
        ),
        confidence=0.80,
    ),
]


# ─── Grand slam detection ───────────────────────────────────────────────

_GRAND_SLAM_RE = re.compile(r"grand\s+slam", re.IGNORECASE)
_MULTI_RUN_RE = re.compile(
    r"(?:two|three|four|2|3|4)\s*(?:-|\s)?run", re.IGNORECASE,
)
_RUN_COUNT = {"two": 2, "three": 3, "four": 4, "2": 2, "3": 3, "4": 4}


def _estimate_runs(text: str, event_type: EventType) -> int:
    """Estimate runs scored from the broadcast text."""
    if event_type not in SCORING_EVENTS:
        return 0
    if _GRAND_SLAM_RE.search(text):
        return 4
    m = _MULTI_RUN_RE.search(text)
    if m:
        word = m.group(0).split("-")[0].split()[0].strip().lower()
        return _RUN_COUNT.get(word, 1)
    # Count explicit "scores" mentions
    scores = len(re.findall(r"\bscores?\b", text, re.IGNORECASE))
    if scores > 0:
        return max(scores, 1)
    if event_type == EventType.HOME_RUN:
        return 1  # solo homer minimum
    return 1


# ─── Player name extraction ─────────────────────────────────────────────

_PLAYER_NAME_RE = re.compile(
    r"(?:by|from|for)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z']+)+)"
)


def _extract_player(text: str) -> str | None:
    m = _PLAYER_NAME_RE.search(text)
    return m.group(1) if m else None


# ─── Main extraction ────────────────────────────────────────────────────

def extract_events(
    text: str,
    *,
    timestamp: float = 0.0,
    min_confidence: float = 0.0,
) -> list[BroadcastEvent]:
    """Extract all detected baseball events from a transcript chunk.

    Parameters
    ----------
    text : str
        Raw transcribed text from one audio chunk.
    timestamp : float
        Seconds since stream start (for the event timestamp).
    min_confidence : float
        Filter out events below this confidence threshold.
    """
    if not text or not text.strip():
        return []

    events: list[BroadcastEvent] = []
    seen_types: set[EventType] = set()

    for pattern in _PATTERNS:
        if pattern.event_type in seen_types:
            continue
        match = pattern.regex.search(text)
        if not match:
            continue

        runs = _estimate_runs(text, pattern.event_type)
        is_scoring = pattern.scoring or runs > 0

        event = BroadcastEvent(
            event_type=pattern.event_type,
            description=match.group(0).strip(),
            confidence=pattern.confidence,
            timestamp=timestamp,
            player_name=_extract_player(text),
            runs_on_play=runs,
            is_scoring=is_scoring,
            raw_text=text.strip(),
        )

        if event.confidence >= min_confidence:
            events.append(event)
            seen_types.add(pattern.event_type)

    return events


def extract_scoring_events(
    text: str, *, timestamp: float = 0.0,
) -> list[BroadcastEvent]:
    """Convenience: only return events where runs scored."""
    return [e for e in extract_events(text, timestamp=timestamp) if e.is_scoring]


# ─── Deduplication for streaming use ─────────────────────────────────────

class EventDeduplicator:
    """Tracks recent events to avoid double-counting from overlapping
    audio chunks. Two events are "same" if they have the same type
    and occur within ``window_seconds`` of each other."""

    def __init__(self, window_seconds: float = 15.0):
        self.window = window_seconds
        self._recent: list[tuple[float, EventType, str]] = []

    def filter_new(self, events: list[BroadcastEvent]) -> list[BroadcastEvent]:
        now = time.time()
        # Expire old entries
        self._recent = [
            (ts, et, desc) for ts, et, desc in self._recent
            if now - ts < self.window
        ]
        new = []
        for e in events:
            is_dup = any(
                et == e.event_type and desc == e.description
                for _, et, desc in self._recent
            )
            if not is_dup:
                new.append(e)
                self._recent.append((now, e.event_type, e.description))
        return new


# ─── CLI test ────────────────────────────────────────────────────────────

def main() -> int:

    test_phrases = [
        "And that ball is GONE! A two-run homer by Judge into the upper deck!",
        "Soto walks on four pitches. Runners at first and second.",
        "Ground ball to short, flips to second, on to first — double play!",
        "Lindor scores from third on the sacrifice fly to center!",
        "Strike three called! Caught him looking on the outside corner.",
        "And there's a wild pitch! Runner comes home from third!",
        "Pitching change — Díaz is coming in from the bullpen.",
        "Error by the shortstop! Ball gets through into left field.",
        "That's the ballgame! Yankees win it 5 to 3.",
        "Grand slam! Alonso clears the bases with a shot to right!",
        "Henderson scores on the single by Machado.",
        "He steals second base! Good jump on the pitcher.",
        "And we go to the bottom of the seventh inning.",
        "Just a routine flyout to center. Nothing happening here.",
    ]

    print("=== Baseball Broadcast Event Extractor ===\n")
    for phrase in test_phrases:
        events = extract_events(phrase)
        if events:
            for e in events:
                scoring = " 🏃 SCORING" if e.is_scoring else ""
                runs = f" ({e.runs_on_play} run{'s' if e.runs_on_play != 1 else ''})" if e.runs_on_play else ""
                print(f"  [{e.event_type.name:>16s}] {e.confidence:.0%} {e.description[:50]}{runs}{scoring}")
        else:
            print(f"  {'[no event]':>19s} {phrase[:60]}")
        print(f"    └─ \"{phrase[:70]}\"")
        print()

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
