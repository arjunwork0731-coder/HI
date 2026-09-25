"""Text normalisation helpers shared by retrieval and deterministic verification.

Everything here is deterministic and dependency-free so that verification
results are reproducible and do not depend on a model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

STOPWORDS = set(
    """a an the and or but if then else of to in on at by for with from into onto over under about as is are was were be been
    being am do does did doing have has had having it its this that these those there here what which who whom whose when where
    why how all any both each few more most other some such only own same so than too very can will just should would could
    may might must shall i me my we our you your he him his she her they them their also not no nor via per upon than
    tell give show explain describe please let us know list find get make much many one's""".split()
)

NEGATIONS = {"not", "no", "never", "cannot", "neither", "nor", "none", "false", "myth", "misconception", "incorrect", "untrue", "nobody", "nothing"}

# Small antonym lexicon used to catch contradictions that have no explicit negation word.
ANTONYMS = [
    ({"crash", "crashed", "crashes", "failed", "failure", "fail", "unsuccessful", "hard-landed"}, {"soft-landed", "successfully", "successful", "success", "soft", "succeeded"}),
    ({"visible"}, {"invisible", "not visible"}),
    ({"increase", "increased", "grew", "rise", "rose", "growth"}, {"decrease", "decreased", "fell", "decline", "declined", "drop", "dropped"}),
    ({"profit"}, {"loss"}),
    ({"liquid"}, {"solid", "gas"}),
    ({"allowed", "permitted", "eligible"}, {"prohibited", "forbidden", "ineligible", "not"}),
    ({"largest", "biggest"}, {"smallest"}),
    ({"closest", "nearest"}, {"farthest", "furthest"}),
]

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9

NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "twice": 2, "dozen": 12,
}
SCALES = {"thousand": 1e3, "lakh": 1e5, "lakhs": 1e5, "million": 1e6, "crore": 1e7, "crores": 1e7, "billion": 1e9, "trillion": 1e12, "k": 1e3, "mn": 1e6, "bn": 1e9, "cr": 1e7}

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-_.]*[a-z0-9]|[a-z0-9]")


def stem(w: str) -> str:
    if len(w) <= 4 or w[0].isdigit():
        return w
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("ly", ""), ("s", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + rep
    return w


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    text = text.lower().replace("’", "'").replace("₹", " inr ")
    text = re.sub(r"'s\b", "", text)  # possessives: novatek's -> novatek
    out = []
    for w in _WORD_RE.findall(text):
        w = w.strip(".-_")
        if not w:
            continue
        if not keep_stop and w in STOPWORDS:
            continue
        out.append(stem(w))
        if "-" in w:  # also index the parts of hyphenated words: soft-landed -> soft, land
            for part in w.split("-"):
                if part and part not in STOPWORDS and not part.isdigit():
                    out.append(stem(part))
    return out


def content_tokens(text: str) -> list[str]:
    """Content words, excluding numbers and month names (handled by quantity checks)."""
    out = []
    for t in tokens(text):
        if re.fullmatch(r"[\d.,:]+", t) or t in MONTHS or t in NUM_WORDS:
            continue
        if "-" in t and not re.search(r"\d", t):
            continue  # alphabetic compounds (soft-landed) are represented by their parts
        out.append(t)
    return out


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    # protect common abbreviations
    protected = re.sub(r"\b(e\.g|i\.e|etc|vs|approx|Dr|Mr|Ms|No|Rs|St)\.", lambda m: m.group(0).replace(".", "§"), text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Za-z0-9\"“(\[₹$])", protected)
    return [p.replace("§", ".").strip() for p in parts if p.strip()]


# ----------------------------------------------------------------------------- quantities

@dataclass
class Quantity:
    value: float | str  # float for numbers, ISO string for dates
    kind: str  # date | year | percent | money | number
    raw: str

    def matches(self, other: "Quantity") -> bool:
        if self.kind == "date" or other.kind == "date":
            if self.kind == other.kind == "date":
                # a less specific claim ("March 2023") matches a more specific source date
                return str(other.value).startswith(str(self.value))
            # a bare year in the claim matches a source date in that year (not vice versa:
            # a specific claimed date is not supported by a bare year)
            if self.kind == "year" and other.kind == "date":
                return str(other.value).startswith(str(int(self.value)))
            return False
        if (self.kind == "year") != (other.kind == "year"):
            return False
        if (self.kind == "percent") != (other.kind == "percent"):
            return False
        a, b = float(self.value), float(other.value)
        if a == b or abs(a - b) <= 1e-6 * max(abs(a), abs(b)):
            return True
        # the claim may be a correct rounding of a more precise source value:
        # claim "26.5" vs source "26.53", claim "88" vs source "87.97"
        m = re.search(r"\.(\d+)", self.raw.replace(",", ""))
        decimals = len(m.group(1)) if m else 0
        src_m = re.search(r"\.(\d+)", other.raw.replace(",", ""))
        src_decimals = len(src_m.group(1)) if src_m else 0
        if src_decimals > decimals:
            scale = a / float(re.sub(r"[^\d.\-]", "", self.raw.replace(",", "")) or a) if a else 1
            return abs(a - b) <= 0.5 * (10 ** -decimals) * abs(scale) + 1e-9
        return False

    def same_kind(self, other: "Quantity") -> bool:
        groups = {"date": "t", "year": "t", "percent": "p", "money": "n", "number": "n"}
        return groups[self.kind] == groups[other.kind]


_DATE_PATTERNS = [
    # 23 August 2023 / 23rd Aug, 2023
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?,?\s+(\d{4})\b", re.I), "dmy"),
    # August 23, 2023
    (re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I), "mdy"),
    # 2023-08-23
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
]
_MONTH_YEAR = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?,?\s+(\d{4})\b", re.I)

_NUM_RE = re.compile(
    r"(?P<cur>₹|\$|€|£|rs\.?\s*|inr\s*|usd\s*)?(?P<num>-?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)(?:\s*(?P<pct>%|percent|per cent))?(?:\s*(?P<scale>thousand|lakhs?|million|crores?|billion|trillion|bn|mn|cr|k)\b)?",
    re.I,
)
_FY_RE = re.compile(r"\bFY\s?'?(\d{4}|\d{2})(?:-\d{2,4})?\b", re.I)


def extract_quantities(text: str) -> list[Quantity]:
    """Extract normalised dates, years, percentages, money amounts and plain numbers."""
    if not text:
        return []
    out: list[Quantity] = []
    t = text
    for pat, style in _DATE_PATTERNS:
        for m in pat.finditer(t):
            try:
                if style == "dmy":
                    d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower().rstrip(".")], int(m.group(3))
                elif style == "mdy":
                    mo, d, y = MONTHS[m.group(1).lower().rstrip(".")], int(m.group(2)), int(m.group(3))
                else:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 1 <= d <= 31 and 1 <= mo <= 12:
                    out.append(Quantity(f"{y:04d}-{mo:02d}-{d:02d}", "date", m.group(0)))
            except (KeyError, ValueError):
                continue
        t = pat.sub(" ", t)
    for m in _MONTH_YEAR.finditer(t):
        mo = MONTHS.get(m.group(1).lower().rstrip("."))
        if mo:
            out.append(Quantity(f"{int(m.group(2)):04d}-{mo:02d}", "date", m.group(0)))
    t = _MONTH_YEAR.sub(" ", t)
    for m in _FY_RE.finditer(t):
        y = m.group(1)
        y = int(y) + 2000 if len(y) == 2 else int(y)
        out.append(Quantity(float(y), "year", m.group(0)))
    t = _FY_RE.sub(" ", t)
    t = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", t)  # clock times
    # remove identifiers like "Chandrayaan-3", "LVM3-M4", "gpt-4o", "E12", "v2" so they are not treated as quantities
    t = re.sub(r"\b[A-Za-z]+[\w]*-\d+[\w-]*\b|\b[A-Za-z]+\d+[A-Za-z0-9]*\b", " ", t)
    for m in _NUM_RE.finditer(t):
        raw = m.group(0).strip()
        num_s = m.group("num").replace(",", "")
        try:
            val = float(num_s)
        except ValueError:
            continue
        scale = m.group("scale")
        cur = m.group("cur")
        if m.group("pct"):
            out.append(Quantity(val, "percent", raw))
            continue
        if scale:
            val = val * SCALES[scale.lower()]
        is_int_like = "." not in num_s and "," not in m.group("num")
        if is_int_like and not cur and not scale and 1800 <= val <= 2100:
            out.append(Quantity(val, "year", raw))
        else:
            out.append(Quantity(val, "money" if (cur or scale) else "number", raw))
    # number words ("fourth country", "two landers")
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in NUM_WORDS and w not in {"one", "second"}:  # 'one'/'second' are too ambiguous in prose
            out.append(Quantity(float(NUM_WORDS[w]), "number", w))
    for m in re.finditer(r"\b(\d+)(?:st|nd|rd|th)\b", text):
        pass  # ordinals like "4th" already captured by _NUM_RE as plain numbers
    return out


def has_negation(text: str) -> bool:
    toks = set(re.findall(r"[a-z']+", text.lower()))
    if toks & NEGATIONS:
        return True
    return bool(re.search(r"n't\b", text.lower()))


def antonym_conflict(a: str, b: str) -> str | None:
    """Return a short description if a and b use opposing words from the lexicon."""
    la, lb = a.lower(), b.lower()
    wa, wb = set(re.findall(r"[a-z\-]+", la)), set(re.findall(r"[a-z\-]+", lb))
    for left, right in ANTONYMS:
        if (wa & left and wb & right and not wa & right) or (wa & right and wb & left and not wa & left):
            hit_a = sorted((wa & (left | right)))
            hit_b = sorted((wb & (left | right)))
            return f"'{', '.join(hit_a)}' vs '{', '.join(hit_b)}'"
    return None


def capitalized_entities(text: str) -> set[str]:
    """Capitalised words that are likely named entities (ignores sentence-initial word)."""
    ents = set()
    for sent in split_sentences(text):
        words = re.findall(r"[A-Za-z][A-Za-z0-9\-]+", sent)
        for i, w in enumerate(words):
            if i == 0:
                continue
            if w[0].isupper() and w.lower() not in STOPWORDS and w.lower() not in MONTHS and len(w) > 2:
                ents.add(w.lower())
    return ents


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
