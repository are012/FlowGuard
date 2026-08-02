"""Shared numeric privacy rules for the AI label-classification boundary."""

from __future__ import annotations

import re

_ARABIC_NUMERIC_EXPRESSION = re.compile(r"\d+(?:[\s,./:\-%]*\d+)*")
_KOREAN_NUMBER_CHARACTERS = r"영공일이삼사오육칠팔구십백천만억"
_KOREAN_NUMBER_WORD = rf"[{_KOREAN_NUMBER_CHARACTERS}]+"
_KOREAN_NUMBER_WITH_UNIT = re.compile(
    rf"{_KOREAN_NUMBER_WORD}\s*(?:원|년|월|일|개월|주|시간|분|초|퍼센트|프로)"
)
_KOREAN_NUMBER_WITH_MAGNITUDE = re.compile(
    rf"(?=[{_KOREAN_NUMBER_CHARACTERS}]{{2,}})"
    rf"(?=[{_KOREAN_NUMBER_CHARACTERS}]*[십백천만억]){_KOREAN_NUMBER_WORD}"
)
_KOREAN_DAY_WORD = re.compile(
    r"(?:하루|이틀|사흘|나흘|닷새|엿새|이레|여드레|아흐레|열흘|"
    r"열\s*(?:하루|이틀|사흘|나흘))"
)
_KOREAN_NATIVE_NUMBER = (
    r"(?:스물|스무|서른|마흔|쉰|예순|일흔|여든|아흔)"
    r"(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉)?|"
    r"열(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉)?|"
    r"(?:하나|둘|셋|넷|한|두|세|네|다섯|여섯|일곱|여덟|아홉)"
)
_KOREAN_NATIVE_NUMBER_WITH_UNIT = re.compile(
    rf"(?:{_KOREAN_NATIVE_NUMBER})\s*"
    r"(?:(?:만|억)\s*)?(?:원|년|월|달|일|개월|주|시간|시|분|초|퍼센트|프로)"
)
_KOREAN_RELATIVE_DATE = re.compile(
    r"(?:오늘|내일|모레|어제|그제|금일|익일|명일|"
    r"올해|내년|작년|재작년|금년|전년|"
    r"이번\s*(?:주|달|월|분기|해|년)|다음\s*(?:주|달|월|분기|해|년)|"
    r"지난\s*(?:주|달|월|분기|해|년)|전\s*분기|"
    r"(?:월|화|수|목|금|토|일)요일)"
)
_ENGLISH_NUMBER_WORD = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|thousand|million|billion|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth)"
)
_ENGLISH_NUMBER_SEQUENCE = (
    rf"{_ENGLISH_NUMBER_WORD}"
    rf"(?:(?:[\s-]+(?:and[\s-]+)?){_ENGLISH_NUMBER_WORD})*"
)
_ENGLISH_NUMBER_WITH_UNIT = re.compile(
    rf"\b{_ENGLISH_NUMBER_SEQUENCE}"
    r"[\s-]*(?:dollars?|euros?|pounds?|yen|won|cents?|percent|percentage|"
    r"years?|quarters?|months?|weeks?|days?|hours?|minutes?|seconds?)\b",
    flags=re.IGNORECASE,
)
_ENGLISH_CURRENCY_AMOUNT = re.compile(
    rf"(?:[$€£¥₩]\s*{_ENGLISH_NUMBER_SEQUENCE}\b|"
    rf"\b(?:USD|EUR|GBP|JPY|KRW)\s+{_ENGLISH_NUMBER_SEQUENCE}\b|"
    rf"\b{_ENGLISH_NUMBER_SEQUENCE}\s*(?:USD|EUR|GBP|JPY|KRW|[$€£¥₩]))",
    flags=re.IGNORECASE,
)
_ENGLISH_DATE_WORD = re.compile(
    r"\b(?:"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"today|tomorrow|yesterday|tonight|"
    r"(?:this|next|last)\s+(?:week|month|quarter|year|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r")\b",
    flags=re.IGNORECASE,
)
_ENGLISH_MAY_MONTH = re.compile(r"\bMay\b")
_REPEATED_REDACTION = re.compile(r"(?:<NUM>\s*){2,}")

_NUMERIC_PATTERNS = (
    _ARABIC_NUMERIC_EXPRESSION,
    _KOREAN_NATIVE_NUMBER_WITH_UNIT,
    _KOREAN_NUMBER_WITH_UNIT,
    _KOREAN_NUMBER_WITH_MAGNITUDE,
    _KOREAN_DAY_WORD,
    _KOREAN_RELATIVE_DATE,
    _ENGLISH_CURRENCY_AMOUNT,
    _ENGLISH_NUMBER_WITH_UNIT,
    _ENGLISH_DATE_WORD,
    _ENGLISH_MAY_MONTH,
)


def contains_numeric_expression(value: str) -> bool:
    """Detect an amount, date, duration, percentage, or other numeric claim."""

    return any(pattern.search(value) for pattern in _NUMERIC_PATTERNS)


def redact_numeric_expressions(value: str) -> str:
    """Remove numeric expressions from free-form label text before AI egress."""

    redacted = value
    for pattern in _NUMERIC_PATTERNS:
        redacted = pattern.sub("<NUM>", redacted)
    redacted = _REPEATED_REDACTION.sub("<NUM> ", redacted)
    return redacted.strip() or "<NUM>"


__all__ = ["contains_numeric_expression", "redact_numeric_expressions"]
