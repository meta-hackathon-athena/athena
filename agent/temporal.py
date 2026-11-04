"""Deterministic time-expression parsing and interval overlap.

"tomorrow at six" -> date = tomorrow, start candidates = [18:00, 06:00] (am/pm unknown),
end = unknown. Nothing is guessed silently: ambiguity and missing parts stay explicit,
and overlap is computed here, never by an LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
       "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "an": 1, "a": 1}
MINUTE_WORDS = {"o'clock": 0, "oclock": 0, "fifteen": 15, "thirty": 30, "forty five": 45, "forty-five": 45}
H = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
AMPM = r"(a\.?m\.?|p\.?m\.?)"
PM_WORDS = ("tonight", "evening", "afternoon", "dinner")
AM_WORDS = ("morning", "breakfast")


def _n(s: str) -> int:
    return int(s) if s.isdigit() else NUM[s]


@dataclass
class TimeSpec:
    date: date | None = None
    date_text: str | None = None
    date_ambiguous: bool = False     # "next Friday", "on Friday" said on a Friday
    date_assumed: bool = False       # no day mentioned; assumed the next time it happens
    starts: list[int] = field(default_factory=list)   # candidate start minutes, most plausible first
    end_min: int | None = None
    time_text: str | None = None

    @property
    def has_time(self) -> bool:
        return bool(self.starts)

    @property
    def time_ambiguous(self) -> bool:
        return len(self.starts) > 1

    @property
    def resolved(self) -> bool:
        """Date and a single start time known, nothing assumed."""
        return (self.date is not None and len(self.starts) == 1
                and not self.date_ambiguous and not self.date_assumed)

    def intervals(self, tz: ZoneInfo) -> list[tuple[int, int | None]]:
        """One (start_ms, end_ms | None) per candidate start. End is None when unknown."""
        if self.date is None or not self.starts:
            return []
        out = []
        for s in self.starts:
            start = datetime.combine(self.date, time(), tz) + timedelta(minutes=s)
            end = None
            if self.end_min is not None:
                e = self.end_min
                while e <= s:
                    e += 12 * 60
                if e - s > 12 * 60:          # "from 6 to 7" read as 6 AM start -> 7 AM end
                    e -= 12 * 60
                end = datetime.combine(self.date, time(), tz) + timedelta(minutes=e)
            out.append((_ms(start), _ms(end) if end else None))
        return out


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fmt_min(m: int) -> str:
    h, mm = divmod(m % (24 * 60), 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d} {suffix}" if mm else f"{h12} {suffix}"


# --------------------------------------------------------------------- dates
def _parse_date(low: str, today: date) -> tuple[date | None, str | None, bool]:
    if m := re.search(r"\bday after tomorrow\b", low):
        return today + timedelta(days=2), m.group(0), False
    if m := re.search(r"\btomorrow\b", low):
        return today + timedelta(days=1), m.group(0), False
    if m := re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", low):
        return today, m.group(0), False
    if m := re.search(r"\b(this |next |on )?(" + "|".join(WEEKDAYS) + r")\b", low):
        target = WEEKDAYS.index(m.group(2))
        ahead = (target - today.weekday()) % 7
        mod = (m.group(1) or "").strip()
        ambiguous = False
        if ahead == 0:
            # "on Friday" said on a Friday: today or in a week? "this Friday" = today.
            ahead, ambiguous = (0, False) if mod == "this" else (7, True)
        if mod == "next":
            d = today + timedelta(days=ahead)
            if d.isocalendar()[1] == today.isocalendar()[1]:
                d += timedelta(days=7)
            return d, m.group(0), True       # people disagree on what "next Friday" means
        return today + timedelta(days=ahead), m.group(0), ambiguous
    mon = "|".join(MONTHS)
    m = (re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?: of)? ({mon})\b", low)
         or re.search(rf"\b({mon}) (\d{{1,2}})(?:st|nd|rd|th)?\b", low))
    if m:
        a, b = m.groups()
        day_s, mon_s = (a, b) if a.isdigit() else (b, a)
        try:
            d = date(today.year, MONTHS.index(mon_s) + 1, int(day_s))
        except ValueError:
            return None, None, False
        if d < today:
            d = d.replace(year=today.year + 1)
        return d, m.group(0), False
    return None, None, False


# --------------------------------------------------------------------- times
def _clock(hour_s: str, minute: int, ampm: str | None, period_pm: bool | None) -> list[int] | None:
    h = _n(hour_s)
    if not (0 <= h <= 23 and 0 <= minute < 60):
        return None
    if ampm:
        if h > 12:
            return None
        h = h % 12 + (12 if ampm.startswith("p") else 0)
        return [h * 60 + minute]
    if h == 0 or h > 12 or (hour_s.isdigit() and len(hour_s) == 2 and hour_s.startswith("0")):
        return [h * 60 + minute]                          # 18:00, 06:30 -> 24h clock
    if period_pm is not None:
        hh = h % 12 + (12 if period_pm else 0)
        return [hh * 60 + minute]
    if h == 12:
        return [12 * 60 + minute]
    am, pm = h * 60 + minute, (h + 12) * 60 + minute
    return [pm, am] if h <= 6 else [am, pm]               # "at six" -> 6 PM more likely, still ambiguous


def _parse_one_time(s: str, period_pm: bool | None) -> tuple[list[int], str] | None:
    if m := re.search(r"\b(noon|midday)\b", s):
        return [12 * 60], m.group(0)
    if m := re.search(r"\bmidnight\b", s):
        return [0], m.group(0)
    if m := re.search(rf"\bhalf past {H}\b", s):
        c = _clock(m.group(1), 30, None, period_pm)
        return (c, m.group(0)) if c else None
    if m := re.search(rf"\bquarter (past|to) {H}\b", s):
        c = _clock(m.group(2), 15, None, period_pm)
        if c and m.group(1) == "to":
            c = [x - 30 for x in c]
        return (c, m.group(0)) if c else None
    if m := re.search(rf"\b(\d{{1,2}})[:.](\d{{2}})\s*{AMPM}?", s):
        c = _clock(m.group(1), int(m.group(2)), m.group(3), period_pm)
        return (c, m.group(0).strip()) if c else None
    if m := re.search(rf"\b{H}\s*{AMPM}", s):
        c = _clock(m.group(1), 0, m.group(2), period_pm)
        return (c, m.group(0)) if c else None
    words = "|".join(MINUTE_WORDS)
    if m := re.search(rf"\b(?:at|around|by|from) {H}(?: ({words}))?\b", s):
        c = _clock(m.group(1), MINUTE_WORDS.get(m.group(2) or "", 0), None, period_pm)
        return (c, m.group(0)) if c else None
    return None


def parse_time_expression(text: str, now: datetime) -> TimeSpec | None:
    """Find a date and/or time in free text. Returns None when there is neither."""
    low = " " + text.lower().replace("’", "'") + " "
    spec = TimeSpec()
    spec.date, spec.date_text, spec.date_ambiguous = _parse_date(low, now.date())

    period_pm = True if any(w in low for w in PM_WORDS) else (False if any(w in low for w in AM_WORDS) else None)

    if m := re.search(r"\bin (\d+|an|a|half an) (minutes?|hours?)\b", low):
        n = 0.5 if m.group(1) == "half an" else _n(m.group(1))
        t = now + timedelta(minutes=n * (60 if m.group(2).startswith("hour") else 1))
        spec.date = spec.date or t.date()
        spec.starts, spec.time_text = [t.hour * 60 + t.minute], m.group(0)
        return spec

    # ranges: "from 6 to 7pm", "6-7pm", "between 2 and 3"
    rng = re.search(rf"\b(?:from |between )?{H}(?:[:.](\d{{2}}))?\s*{AMPM}?\s*(?:-|–|to|until|till|and)\s*"
                    rf"{H}(?:[:.](\d{{2}}))?\s*{AMPM}?", low)
    if rng and (rng.group(0).startswith(("from", "between")) or rng.group(3) or rng.group(6)
                or re.search(r"\d", rng.group(0))):
        h1, m1, ap1, h2, m2, ap2 = rng.groups()
        ap1 = ap1 or ap2                                   # "6-7pm": pm applies to both
        s = _clock(h1, int(m1 or 0), ap1, period_pm)
        e = _clock(h2, int(m2 or 0), ap2, period_pm)
        if s and e:
            spec.starts, spec.end_min, spec.time_text = s, e[0], rng.group(0).strip()
    if not spec.starts:
        one = _parse_one_time(low, period_pm)
        if one:
            spec.starts, spec.time_text = one
            if m := re.search(rf"\b(?:until|till) {H}(?:[:.](\d{{2}}))?\s*{AMPM}?", low):
                e = _clock(m.group(1), int(m.group(2) or 0), m.group(3), period_pm)
                if e:
                    spec.end_min = e[0]
            elif m := re.search(r"\bfor (an|one|two|three|\d+|half an) (hour|hours|minutes)\b", low):
                n = 0.5 if m.group(1) == "half an" else _n(m.group(1))
                spec.end_min = spec.starts[0] + int(n * (60 if m.group(2).startswith("hour") else 1))

    if spec.date is None and not spec.starts:
        return None

    now_min = now.hour * 60 + now.minute
    if spec.date is None:
        # "at six" with no day: the next time it happens. Marked as assumed.
        spec.date_assumed = True
        spec.date = now.date() if any(s > now_min for s in spec.starts) else now.date() + timedelta(days=1)
    if spec.date == now.date() and len(spec.starts) > 1:
        future = [s for s in spec.starts if s > now_min]
        if future:                                       # 6 AM already passed -> must be 6 PM
            spec.starts = future
    return spec


# ------------------------------------------------------------------ overlap
NEAR_MS = 60 * 60_000   # an event of unknown length starting <1h before another *may* overlap it


def overlap(a_start: int, a_end: int | None, b_start: int, b_end: int | None) -> str | None:
    """'conflict' when intervals certainly overlap, 'possible' when they might
    (an end time is unknown and the starts are close), else None."""
    if a_end is not None and b_end is not None:
        return "conflict" if a_start < b_end and b_start < a_end else None
    if a_start == b_start:
        return "conflict"
    if b_end is not None and b_start <= a_start < b_end:
        return "conflict"            # a starts inside b
    if a_end is not None and a_start <= b_start < a_end:
        return "conflict"            # b starts inside a
    first_start, first_end, second_start = ((a_start, a_end, b_start) if a_start < b_start
                                            else (b_start, b_end, a_start))
    if first_end is None and second_start - first_start < NEAR_MS:
        return "possible"
    return None
