"""Content guard: redact personal data and score prompt injection before text reaches a model or leaves a system.

This is a heuristic filter, not a security boundary. Pattern matching catches the common, careless and
copy-pasted attacks and obvious personal data. A determined attacker rephrases, so treat a clean verdict as
"nothing obvious", never as "safe", and keep the real controls (scope, risk, approval) in the policy layer.

It is built for text that is not only English:

* Digits are normalised before matching, so Arabic-Indic and Persian digits in a card number or phone number
  are caught. Replacement happens on the original text, so nothing else in the message is altered.
* IBANs are validated with the ISO 13616 mod-97 check, so a random digit string is not flagged and a real
  Gulf, European or UK account number is.
* Injection phrases are scored in English, Arabic, Chinese, Spanish, French and Indonesian. Zero-width
  characters and compatibility forms are stripped first, which defeats the cheapest evasions.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Pattern, Tuple

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)\d{3,4}[\s.-]?\d{3,4}(?!\d)")

_DIGIT_AFTER = re.compile(r"[ .-]?\d")
_DIGIT_BEFORE = re.compile(r"\d[ .-]?$")
_DIGITS = {ord(c): str(i) for i, c in enumerate("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669")}  # Arabic-Indic
_DIGITS.update({ord(c): str(i) for i, c in enumerate("\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9")})  # Persian
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u202a\u202b\u202c\u202d\u202e"), None)

# (pattern, weight). Weight 3 alone reaches the default threshold.
INJECTION_PATTERNS: List[Tuple[Pattern[str], int]] = [
    (re.compile(r"ignore (?:all |any )?(?:previous|prior|above) (?:instructions|prompts?)", re.I), 3),
    (re.compile(r"disregard (?:the |your )?(?:system|previous|above)", re.I), 3),
    (re.compile(r"reveal (?:your |the )?(?:system|hidden) prompt", re.I), 3),
    (re.compile(r"you are now (?:in )?(?:developer|dan|jailbreak)", re.I), 2),
    (re.compile(r"pretend (?:you have|there are) no (?:rules|restrictions)", re.I), 2),
    (re.compile(r"</?(?:system|assistant)>", re.I), 1),
    (re.compile(r"ignora (?:todas )?las instrucciones anteriores", re.I), 3),
    (re.compile(r"ignore[zr]? (?:toutes )?les instructions pr[\u00e9e]c[\u00e9e]dentes", re.I), 3),
    (re.compile(r"abaikan (?:semua )?instruksi (?:sebelumnya|di atas)", re.I), 3),
    (re.compile("\u062a\u062c\u0627\u0647\u0644 (?:\u062c\u0645\u064a\u0639 |\u0643\u0644 )?\u0627\u0644\u062a\u0639\u0644\u064a\u0645\u0627\u062a \u0627\u0644\u0633\u0627\u0628\u0642\u0629"), 3),
    (re.compile("\u5ffd\u7565(?:\u4e4b\u524d|\u4ee5\u4e0a|\u5148\u524d|\u4e0a\u9762)\u7684(?:\u6240\u6709)?(?:\u6307\u4ee4|\u6307\u793a|\u63d0\u793a)"), 3),
]


def luhn_valid(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for index, value in enumerate(reversed(nums)):
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def iban_valid(candidate: str) -> bool:
    """ISO 13616: 15 to 34 characters, and the rearranged number is congruent to 1 modulo 97."""
    iban = candidate.replace(" ", "").upper()
    if not 15 <= len(iban) <= 34 or not iban.isalnum() or not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    rearranged = iban[4:] + iban[:4]
    number = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(number) % 97 == 1


def normalise(text: str) -> str:
    """Fold compatibility forms and drop invisible characters, for matching only."""
    return unicodedata.normalize("NFKC", text).translate(_INVISIBLE)


@dataclass
class Finding:
    kind: str
    value: str


_RULES: List[Tuple[str, Pattern[str], Optional[Callable[[str], bool]]]] = [
    ("EMAIL", EMAIL, None),
    ("IBAN", IBAN, iban_valid),
    ("CARD", CARD, luhn_valid),
    ("IP", IPV4, None),
    ("PHONE", PHONE, lambda s: sum(c.isdigit() for c in s) >= 9),
]


def redact(text: str) -> Tuple[str, List[Finding]]:
    """Replace personal data with typed placeholders. Returns (clean_text, findings).

    Matching runs on a digit-normalised copy of the same length, and replacement runs on the original, so a
    message in any script keeps its own words and only the sensitive spans change.
    """
    view = text.translate(_DIGITS)
    spans: List[Tuple[int, int, str]] = []
    for kind, pattern, check in _RULES:
        for match in pattern.finditer(view):
            raw = match.group(0)
            start, end = match.start(), match.start() + len(raw.rstrip(" -"))
            if check is not None and not check(raw):
                continue
            if kind == "PHONE" and (_DIGIT_AFTER.match(view, end) or _DIGIT_BEFORE.search(view[:start])):
                continue  # a slice of a longer digit run, such as a card number that failed its checksum
            if any(start < e and s < end for s, e, _ in spans):
                continue
            spans.append((start, end, kind))
    spans.sort()
    out: List[str] = []
    findings: List[Finding] = []
    cursor = 0
    for start, end, kind in spans:
        out.append(text[cursor:start])
        out.append(f"[{kind}]")
        findings.append(Finding(kind, text[start:end]))
        cursor = end
    out.append(text[cursor:])
    return "".join(out), findings


def injection_score(text: str) -> int:
    folded = normalise(text)
    return sum(weight for pattern, weight in INJECTION_PATTERNS if pattern.search(folded))


@dataclass
class Verdict:
    allowed: bool
    reasons: List[str]
    clean_text: str
    findings: List[Finding] = field(default_factory=list)


@dataclass
class ContentPolicy:
    """Rules for one direction of traffic. ``block_on_pii`` is for outputs, where leaking data is the failure."""

    max_chars: int = 8000
    injection_threshold: int = 3
    blocked_terms: Tuple[str, ...] = ()
    redact_pii: bool = True
    block_on_pii: bool = False

    def check(self, text: str) -> Verdict:
        reasons: List[str] = []
        if len(text) > self.max_chars:
            reasons.append(f"length {len(text)} exceeds {self.max_chars}")
        score = injection_score(text)
        if score >= self.injection_threshold:
            reasons.append(f"injection score {score}")
        lowered = normalise(text).casefold()
        reasons += [f"blocked term: {t}" for t in self.blocked_terms if normalise(t).casefold() in lowered]
        clean, findings = redact(text) if (self.redact_pii or self.block_on_pii) else (text, [])
        if self.block_on_pii and findings:
            reasons.append("personal data: " + ", ".join(sorted({f.kind for f in findings})))
        return Verdict(not reasons, reasons, clean if self.redact_pii else text, findings)


def guard_verifier(policy: ContentPolicy) -> Callable[[Any], bool]:
    """A ``Step.verify`` check. Output that fails the policy is a failed attempt and never reaches a dependent step."""

    def verify(output: Any) -> bool:
        return policy.check(output if isinstance(output, str) else str(output)).allowed

    return verify
