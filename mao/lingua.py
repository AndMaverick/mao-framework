"""Text that is not American English, normalised before it reaches a model, an index or a ledger.

Arabic: the same word is written several ways, so search and deduplication fail quietly. Indonesian: money is
``Rp 1.500.000``, ``1,5 juta`` or ``500 rb``, and chat is full of abbreviations. Gulf currencies: dirham, riyal,
dinar amounts in Latin or Arabic script. Script profiling routes mixed-language input.

Standard library only. Non-ASCII literals are written as escapes so the file reads the same in any editor.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple, Union

Number = Union[int, Decimal]

# --------------------------------------------------------------------------- Arabic

_DIACRITICS = {chr(c) for c in range(0x064B, 0x0656)} | {"ٰ"}
_TATWEEL = "ـ"
_ALEF = {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"}
_DIGITS = {**{chr(0x0660 + i): str(i) for i in range(10)}, **{chr(0x06F0 + i): str(i) for i in range(10)}}
_ARABIC_DECIMAL = {"٫": ".", "٬": ","}


def is_arabic_letter(ch: str) -> bool:
    code = ord(ch)
    return (0x0621 <= code <= 0x064A) or (0x0671 <= code <= 0x06D3) or code in (0x06FA, 0x06FB, 0x06FC)


def normalize(text: str, *, ta_marbuta: bool = False) -> str:
    """Strip diacritics and tatweel, unify alef forms and the final yeh, Eastern digits to ASCII.

    ``ta_marbuta=True`` also folds teh marbuta into heh, which helps recall and costs precision.
    """
    out = []
    for ch in unicodedata.normalize("NFKC", text):
        if ch in _DIACRITICS or ch == _TATWEEL:
            continue
        ch = _ALEF.get(ch, ch)
        ch = _ARABIC_DECIMAL.get(ch, ch)
        ch = _DIGITS.get(ch, ch)
        if ch == "ى":
            ch = "ي"
        if ta_marbuta and ch == "ة":
            ch = "ه"
        out.append(ch)
    return "".join(out)


def search_key(text: str) -> str:
    """A canonical key for lookups and deduplication: normalised, lowercased, single-spaced."""
    return " ".join(normalize(text, ta_marbuta=True).lower().split())


def tokens(text: str) -> List[str]:
    return re.findall(r"[^\W_]+", normalize(text), flags=re.UNICODE)


def arabic_ratio(text: str) -> float:
    """Share of letters that are Arabic."""
    letters = [c for c in text if c.isalpha()]
    return sum(is_arabic_letter(c) for c in letters) / len(letters) if letters else 0.0


# --------------------------------------------------------------------------- scripts

_SCRIPTS = ("LATIN", "ARABIC", "CJK", "CYRILLIC", "HANGUL", "HIRAGANA", "KATAKANA", "DEVANAGARI", "THAI", "HEBREW", "GREEK")


def script_profile(text: str) -> Dict[str, float]:
    """Share of letters in each script, largest first. Useful for routing mixed-language input."""
    counts: Dict[str, int] = {}
    total = 0
    for ch in text:
        if not ch.isalpha():
            continue
        total += 1
        name = unicodedata.name(ch, "")
        script = next((s for s in _SCRIPTS if name.startswith(s)), "OTHER")
        counts[script] = counts.get(script, 0) + 1
    return {k: round(v / total, 4) for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))} if total else {}


def dominant_script(text: str, minimum: float = 0.5) -> Optional[str]:
    """The script holding at least ``minimum`` of the letters, else None (mixed input)."""
    profile = script_profile(text)
    if not profile:
        return None
    name, share = next(iter(profile.items()))
    return name if share >= minimum else None


# --------------------------------------------------------------------------- Gulf amounts

_CURRENCY_WORDS = {
    "aed": "AED", "dirham": "AED", "dirhams": "AED", "درهم": "AED", "دراهم": "AED",
    "sar": "SAR", "riyal": "SAR", "riyals": "SAR", "ريال": "SAR", "ريالا": "SAR",
    "qar": "QAR", "kwd": "KWD", "bhd": "BHD", "omr": "OMR",
    "دينار": "KWD",
    "usd": "USD", "dollar": "USD", "dollars": "USD", "$": "USD",
}
_MULTIPLIERS = {"thousand": 1_000, "k": 1_000, "million": 1_000_000, "m": 1_000_000, "billion": 1_000_000_000, "bn": 1_000_000_000,
                "الف": 1_000, "مليون": 1_000_000, "مليار": 1_000_000_000}
_WORD = r"[A-Za-zء-يٱ-ۓ$]+"
_AMOUNT = re.compile(rf"(?:(?P<pre>{_WORD})\s*)?(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<mult>{_WORD})?\s*(?P<post>{_WORD})?")


def parse_amount(text: str) -> Tuple[Number, str]:
    """First money amount in Latin or Arabic script, as (amount, ISO currency). Raises ValueError if there is none.

    Handles ``AED 1,500``, ``1.5 million dirhams``, ``500 SAR`` and Eastern Arabic digits. The currency must be stated.
    """
    cleaned = normalize(text)
    for m in _AMOUNT.finditer(cleaned):
        words = [w.lower() for w in (m.group("pre"), m.group("mult"), m.group("post")) if w]
        currency = next((_CURRENCY_WORDS[w] for w in words if w in _CURRENCY_WORDS), None)
        if currency is None:
            continue
        mult = next((_MULTIPLIERS[w] for w in (m.group("mult"), m.group("post")) if w and w.lower() in _MULTIPLIERS), 1)
        try:
            value = Decimal(m.group("num").replace(",", "")) * mult
        except InvalidOperation:
            continue
        return (int(value) if value == value.to_integral_value() else value), currency
    raise ValueError(f"no amount with a stated currency in: {text!r}")


# --------------------------------------------------------------------------- Indonesian

_UNITS = {"triliun": 10**12, "miliar": 10**9, "milyar": 10**9, "juta": 10**6, "jt": 10**6, "ribu": 10**3, "rb": 10**3, "k": 10**3}
_RUPIAH = re.compile(r"(?i)(?:rp\.?\s*)?(\d[\d.,]*)\s*(triliun|miliar|milyar|juta|jt|ribu|rb|k)?(?![A-Za-z0-9])")
_MONTHS = ["januari", "februari", "maret", "april", "mei", "juni", "juli", "agustus", "september", "oktober", "november", "desember"]
_MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "jun": 6, "jul": 7, "agu": 8, "agt": 8, "sep": 9, "okt": 10, "nov": 11, "des": 12}
_DATE = re.compile(r"(?i)\b(\d{1,2})\s+([a-z]+)\s+(\d{4})\b")
SLANG: Dict[str, str] = {
    "gak": "tidak", "ga": "tidak", "nggak": "tidak", "enggak": "tidak", "tdk": "tidak", "udah": "sudah", "udh": "sudah", "sdh": "sudah",
    "bgt": "sangat", "banget": "sangat", "aja": "saja", "gimana": "bagaimana", "yg": "yang", "dgn": "dengan", "utk": "untuk",
    "krn": "karena", "jg": "juga", "sy": "saya", "emang": "memang", "kalo": "kalau",
}
_LATIN_WORD = re.compile(r"[A-Za-z]+")


def _idr_number(raw: str) -> Decimal:
    raw = raw.strip(".,")
    if "," in raw:
        return Decimal(raw.replace(".", "").replace(",", "."))
    if raw.count(".") > 1 or re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
        return Decimal(raw.replace(".", ""))
    return Decimal(raw)


def parse_rupiah(text: str) -> Number:
    """First rupiah amount: ``Rp 1.500.000``, ``1,5 juta``, ``500 rb``, ``2 miliar``. Raises ValueError if none."""
    for m in _RUPIAH.finditer(text):
        number, unit = m.group(1), m.group(2)
        if not unit and not re.search(r"(?i)rp", m.group(0)) and not re.fullmatch(r"\d{1,3}(\.\d{3})+", number):
            continue
        try:
            value = _idr_number(number) * (_UNITS[unit.lower()] if unit else 1)
        except InvalidOperation:
            continue
        return int(value) if value == value.to_integral_value() else value
    raise ValueError(f"no rupiah amount found in: {text!r}")


def format_rupiah(amount: Union[int, float, Decimal]) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}Rp {abs(round(amount)):,}".replace(",", ".")


def normalize_slang(text: str) -> str:
    """Replace common chat abbreviations with standard Indonesian. Unknown words, including names, are untouched."""
    return _LATIN_WORD.sub(lambda m: SLANG.get(m.group(0).lower(), m.group(0)), text)


def parse_date(text: str) -> date:
    """A date like ``17 Agustus 2026`` or ``17 Agu 2026``. Raises ValueError if none, or if the day does not exist."""
    for m in _DATE.finditer(text):
        day, name, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = _MONTHS.index(name) + 1 if name in _MONTHS else _MONTH_ABBR.get(name) if len(name) == 3 else None
        if month:
            return date(year, month, day)
    raise ValueError(f"no Indonesian date found in: {text!r}")


def format_date(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1].capitalize()} {value.year}"


def normalize_phone(raw: str) -> str:
    """Canonical +62 form. Local, international and bare forms converge. Foreign or implausible numbers raise ValueError."""
    digits = re.sub(r"\D", "", normalize(raw))
    if digits.startswith("62"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    else:
        raise ValueError(f"not an Indonesian number: {raw!r}")
    if digits.startswith("0"):  # +62 (0)812 style
        digits = digits[1:]
    if not 8 <= len(digits) <= 12:
        raise ValueError(f"unexpected length for an Indonesian number: {raw!r}")
    return "+62" + digits
