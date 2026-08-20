"""Identifier/numeric fuse for grounding_version=1 answers.

Lexical only: exact identifiers, numeric values with kPa/MPa equivalence,
and a narrow attachment-observation exception.  Does not infer meaning.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


# Citation markers are not evidence.  Replacing them with a space also keeps
# adjacent words from becoming one token after a marker is removed.
_CITATION_RE = re.compile(r"\[\s*(?:id\s*:\s*)?[0-9]+\s*\]", re.IGNORECASE)
_PROSE_ID_RE = re.compile(r"(?:知识库\s*)?id\s*:\s*[0-9]+", re.IGNORECASE)
# Structural list ordinals only (not decimals like 1.5 MPa / 2.0).
# Matches "1. 手册", "2) 说明", "3、巡检" at line start or after punctuation.
_LIST_NUMBER_RE = re.compile(
    r"(?:(?<=^)|(?<=[\n\r\t ：:;；，,。！？!]))"
    r"[0-9]{1,2}"
    r"(?:"
    r"[.)）](?=[ \t]+[^\s0-9.])"
    r"|、(?=[ \t]*\S)"
    r")"
    r"[ \t]*"
)

_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9._/-])"
    r"[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?"
    r"(?![A-Za-z0-9._/-])"
)

_FRACTION_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<numerator>[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+))"
    r"[ \t]*/[ \t]*"
    r"(?P<denominator>[0-9]+)"
)
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<number>[+-]?(?:(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)"
    r"(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
)

_CHINESE_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<number>[零〇一二两三四五六七八九十百千万亿点]+?)"
    r"[ \t]*(?P<unit>%|千帕|兆帕|帕|千克|公斤|克|吨|千瓦|瓦|伏|安|赫兹|小时|分钟|秒|天|公里|千米|厘米|毫米|米|升|毫升|台|件|套|个|只|次|级|路|条|页|度|℃)"
)

_UNIT_CHARS_RE = re.compile(
    r"(?:[A-Za-zµμΩ°℃]+(?:[ \t]*/[ \t]*[A-Za-zµμΩ°℃]+)?|"
    r"千帕|兆帕|帕|千克|公斤|克|吨|千瓦|瓦|伏|安|赫兹|小时|分钟|秒|天|"
    r"公里|千米|厘米|毫米|米|升|毫升|台|件|套|个|只|次|级|路|条|页|度)"
)
_KNOWN_UNITS = frozenset(
    {
        "%",
        "pa",
        "kpa",
        "mpa",
        "bar",
        "mbar",
        "psi",
        "mmhg",
        "kg",
        "g",
        "mg",
        "t",
        "lb",
        "l",
        "ml",
        "m",
        "m/s",
        "km/h",
        "cm",
        "mm",
        "km",
        "μm",
        "um",
        "nm",
        "s",
        "sec",
        "min",
        "h",
        "hz",
        "khz",
        "mhz",
        "v",
        "kv",
        "a",
        "ka",
        "w",
        "kw",
        "mw",
        "rpm",
        "°c",
        "°f",
        "℃",
        "度",
        "帕",
        "千帕",
        "兆帕",
        "千克",
        "公斤",
        "克",
        "吨",
        "千瓦",
        "瓦",
        "伏",
        "安",
        "赫兹",
        "小时",
        "分钟",
        "秒",
        "天",
        "公里",
        "千米",
        "厘米",
        "毫米",
        "米",
        "升",
        "毫升",
        "台",
        "件",
        "套",
        "个",
        "只",
        "次",
        "级",
        "路",
        "条",
        "页",
    }
)

_ATTACHMENT_SOURCE_RE = re.compile(
    r"附件|上传(?:的)?文件|图片|图像|截图|照片|画面|attachment|image|screenshot",
    re.IGNORECASE,
)
_OBSERVATION_WORD_RE = re.compile(
    r"可见|显示|识别|疑似|观察到|看见|看到|检测到|呈现|visible|shown|"
    r"identified|suspected|detected",
    re.IGNORECASE,
)
_ENTERPRISE_CLAIM_RE = re.compile(
    r"设备(?:当前|目前|现状|运行状态|状态(?:为|是))|"
    r"(?:当前|目前)(?:状态|故障|参数|压力|温度)|台账|工单|维修(?:历史|记录)|"
    r"保养(?:历史|记录)|检修(?:历史|记录)|制度|规定|参数(?:要求|规定|为|是)|"
    r"额定|设定值|"
    r"\b(?:current\s+(?:device|status|fault|parameter)|ledger|work\s*order|"
    r"repair\s+(?:history|record)|maintenance\s+(?:history|record)|policy|"
    r"requirement|parameter\s+requirement|rated|setpoint)\b",
    re.IGNORECASE,
)
_SENTENCE_BREAK_RE = re.compile(r"[。！？!?；;\n]|(?<![0-9])\.(?![0-9])")
# Discourse / retrieval-layer counts are not Content facts.
# Keep real quantity questions like「共3条维修记录」in the FACT path.
_RETRIEVAL_META_SENTENCE_RE = re.compile(
    r"(?:"
    r"(?:找到|检索到|命中|召回|搜到).{0,24}(?:相关)?(?:片段|结果|chunk|文档)"
    r"|共\s*[0-9零〇一二两三四五六七八九十百千两]+\s*(?:类|种)(?![0-9A-Za-z])"
    r"|共\s*[0-9零〇一二两三四五六七八九十百千两]+\s*份(?:资料|文档|文件)?"
    r"|[0-9零〇一二两三四五六七八九十百千两]+\s*条(?:相关)?(?:片段|结果)"
    r"|[0-9零〇一二两三四五六七八九十百千两]+\s*类(?:资料|文档|信息|内容)?"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Occurrence:
    key: tuple[Any, ...]
    start: int
    end: int


@dataclass(frozen=True)
class GroundingResult:
    passed: bool
    unmatched_identifiers: int
    unmatched_numbers: int
    unmatched_number_keys: tuple[str, ...] = ()


def _as_text(value: Any) -> str:
    """Read strings, chunk-like iterables, and attachment observations."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return " ".join(_as_text(item) for item in value)
    preferred = (
        "text_spans",
        "error_codes",
        "equipment_codes",
        "visible_values",
    )
    selected = [getattr(value, name) for name in preferred if hasattr(value, name)]
    if selected:
        return " ".join(_as_text(item) for item in selected)
    return str(value)


def _normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _as_text(value)).replace("−", "-")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _LIST_NUMBER_RE.sub(" ", text)
    text = _CITATION_RE.sub(" ", text)
    text = _PROSE_ID_RE.sub(" ", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip().casefold()


def _number_unit_identifier(token: str) -> bool:
    """Do not treat ordinary forms such as ``2MPa`` as equipment IDs."""
    if not token or not token[0].isdigit():
        return False
    number = r"[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?"
    match = re.fullmatch(
        number + r"(?P<unit>[A-Za-zµμΩ°℃]+(?:/[A-Za-zµμΩ°℃]+)?)",
        token,
    )
    if match:
        return match.group("unit") in _KNOWN_UNITS
    return bool(re.fullmatch(number, token) and "e" in token)


def _identifiers(text: str) -> list[tuple[str, int, int]]:
    result: list[tuple[str, int, int]] = []
    for match in _IDENTIFIER_RE.finditer(text):
        token = match.group(0)
        if len(token) < 3 or _number_unit_identifier(token):
            continue
        if not any(char.isalpha() for char in token) or not any(
            char.isdigit() for char in token
        ):
            continue
        result.append((token, match.start(), match.end()))
    return result


def _mask_identifier_digits(text: str, identifiers: list[tuple[str, int, int]]) -> str:
    chars = list(text)
    for _, start, end in identifiers:
        for index in range(start, end):
            if chars[index].isdigit():
                chars[index] = " "
    return "".join(chars)


def _decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _unit_after(text: str, end: int) -> tuple[str, int]:
    index = end
    while index < len(text) and text[index] in " \t":
        index += 1
    if index < len(text) and text[index] == "%":
        return "%", index + 1

    match = _UNIT_CHARS_RE.match(text, index)
    if not match:
        return "", end
    unit = match.group(0).replace(" ", "").casefold()
    return unit, match.end()


def _number_key(value: Decimal, unit: str) -> tuple[Any, ...]:
    if unit == "kpa":
        return ("pressure", value / Decimal(1000))
    if unit == "mpa":
        return ("pressure", value)
    return ("number", unit, value)


def _fraction_key(numerator: Decimal, denominator: Decimal, unit: str) -> tuple[Any, ...] | None:
    if denominator == 0:
        return None
    return ("fraction", numerator, denominator, unit)


def _overlaps(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(start < other_end and end > other_start for other_start, other_end in occupied)


def _numbers(text: str, identifiers: list[tuple[str, int, int]]) -> list[_Occurrence]:
    masked = _mask_identifier_digits(text, identifiers)
    occurrences: list[_Occurrence] = []
    occupied: list[tuple[int, int]] = []

    for match in _FRACTION_RE.finditer(masked):
        start, end = match.span()
        numerator = _decimal(match.group("numerator"))
        denominator = _decimal(match.group("denominator"))
        if numerator is None or denominator is None:
            continue
        unit, value_end = _unit_after(masked, end)
        key = _fraction_key(numerator, denominator, unit)
        if key is None:
            continue
        occurrences.append(_Occurrence(key, start, value_end))
        occupied.append((start, value_end))

    for match in _NUMBER_RE.finditer(masked):
        start, end = match.span()
        if _overlaps(start, end, occupied):
            continue
        value = _decimal(match.group("number"))
        if value is None:
            continue
        unit, value_end = _unit_after(masked, end)
        occurrences.append(_Occurrence(_number_key(value, unit), start, value_end))
        occupied.append((start, value_end))

    for match in _CHINESE_NUMBER_RE.finditer(text):
        start, end = match.span()
        if _overlaps(start, end, occupied):
            continue
        number = match.group("number")
        unit = match.group("unit").casefold()
        occurrences.append(_Occurrence(("chinese", number + unit), start, end))
        occupied.append((start, end))

    return occurrences


def _sentence(text: str, start: int, end: int) -> str:
    breaks = [match.start() for match in _SENTENCE_BREAK_RE.finditer(text)]
    left = max((point for point in breaks if point < start and not start <= point < end), default=-1)
    right = min((point for point in breaks if point >= end and not start <= point < end), default=len(text))
    return text[left + 1 : right]


def _is_retrieval_meta_number(text: str, start: int, end: int) -> bool:
    """True for retrieval/inventory discourse counts, not Content facts."""
    return bool(_RETRIEVAL_META_SENTENCE_RE.search(_sentence(text, start, end)))


def _attachment_observation_allowed(text: str, start: int, end: int) -> bool:
    sentence = _sentence(text, start, end)
    if not _ATTACHMENT_SOURCE_RE.search(sentence) or not _OBSERVATION_WORD_RE.search(
        sentence
    ):
        return False
    return not _ENTERPRISE_CLAIM_RE.search(sentence)


def _allowed_identifier_tokens(allowed_identifiers: Iterable[str] | None) -> set[str]:
    tokens: set[str] = set()
    for raw in allowed_identifiers or ():
        text = _normalise(raw)
        if not text:
            continue
        extracted = [token for token, _, _ in _identifiers(text)]
        if extracted:
            tokens.update(extracted)
        else:
            tokens.add(text)
    return tokens


def evaluate_grounding(
    answer: str,
    effective_knowledge: str,
    current_attachment_observation: Iterable[Any] | None = None,
    allowed_identifiers: Iterable[str] | None = None,
) -> GroundingResult:
    """Count unmatched identifiers/numbers without logging answer or knowledge text."""
    answer_text = _normalise(answer)
    knowledge_text = _normalise(effective_knowledge)
    attachment_text = _normalise(current_attachment_observation)

    answer_ids = _identifiers(answer_text)
    knowledge_ids = {token for token, _, _ in _identifiers(knowledge_text)}
    knowledge_ids.update(_allowed_identifier_tokens(allowed_identifiers))
    attachment_ids = {token for token, _, _ in _identifiers(attachment_text)}

    unmatched_identifiers = 0
    for token, start, end in answer_ids:
        if token in knowledge_ids:
            continue
        if token in attachment_ids and _attachment_observation_allowed(
            answer_text, start, end
        ):
            continue
        unmatched_identifiers += 1

    answer_numbers = _numbers(answer_text, answer_ids)
    knowledge_numbers = {
        occurrence.key for occurrence in _numbers(knowledge_text, _identifiers(knowledge_text))
    }
    attachment_numbers = {
        occurrence.key
        for occurrence in _numbers(attachment_text, _identifiers(attachment_text))
    }
    unmatched_numbers = 0
    unmatched_number_keys: list[str] = []
    for occurrence in answer_numbers:
        if occurrence.key in knowledge_numbers:
            continue
        if occurrence.key in attachment_numbers and _attachment_observation_allowed(
            answer_text, occurrence.start, occurrence.end
        ):
            continue
        if _is_retrieval_meta_number(answer_text, occurrence.start, occurrence.end):
            continue
        unmatched_numbers += 1
        unmatched_number_keys.append(repr(occurrence.key))

    return GroundingResult(
        passed=unmatched_identifiers == 0 and unmatched_numbers == 0,
        unmatched_identifiers=unmatched_identifiers,
        unmatched_numbers=unmatched_numbers,
        unmatched_number_keys=tuple(unmatched_number_keys),
    )


def is_grounded(
    answer: str,
    effective_knowledge: str,
    current_attachment_observation: Iterable[Any] | None = None,
    allowed_identifiers: Iterable[str] | None = None,
) -> bool:
    """Return whether answer identifiers/numbers are supported by evidence.

    This intentionally checks lexical evidence only.  It does not infer facts,
    resolve contradictions, or decide source authority.  Bound or current-question
    equipment identifiers may be supplied via ``allowed_identifiers``; numbers
    still require knowledge or an allowed attachment observation.
    """
    return evaluate_grounding(
        answer,
        effective_knowledge,
        current_attachment_observation,
        allowed_identifiers=allowed_identifiers,
    ).passed


STANDARD_ABSTAIN_ANSWER = "未找到可靠依据，无法回答。"

# Model often invents placeholder readings like "0 Hz / 0 A / 0 °C" for inventory asks.
_ZERO_MEASUREMENT_RE = re.compile(
    r"(?<![0-9.])"
    r"0(?:\.0+)?"
    r"[ \t]*"
    r"(?:"
    r"%|hz|khz|mhz|rpm|"
    r"a|ka|ma|μa|ua|v|kv|mv|"
    r"w|kw|mw|"
    r"pa|kpa|mpa|bar|mbar|psi|"
    r"°c|°f|℃|度|"
    r"kg|g|mg|t|"
    r"m|cm|mm|km|μm|um|"
    r"s|sec|min|h|"
    r"千帕|兆帕|帕|千克|公斤|克|吨|千瓦|瓦|伏|安|赫兹|小时|分钟|秒|"
    r"公里|千米|厘米|毫米|米|升|毫升"
    r")(?![a-z0-9µμ])",
    re.IGNORECASE,
)


def visible_answer_text(answer: str) -> str:
    text = answer or ""
    if "</think>" not in text:
        return text
    return text.rsplit("</think>", 1)[-1]


def empty_reference() -> dict:
    return {"chunks": [], "doc_aggs": []}


def _unmatched_keys_are_zero_only(result: GroundingResult) -> bool:
    keys = result.unmatched_number_keys or ()
    if not keys or result.unmatched_identifiers:
        return False
    return all(re.search(r"Decimal\('0(?:\.0+)?'\)", key) for key in keys)


def strip_ungrounded_zero_measurements(answer: str) -> str:
    """Drop placeholder 0/0.0+unit readings from the visible answer text."""
    text = answer or ""
    if "</think>" in text:
        think, visible = text.rsplit("</think>", 1)
        cleaned = _ZERO_MEASUREMENT_RE.sub(" ", visible)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+([，,；;。！？!?])", r"\1", cleaned)
        return think + "</think>" + cleaned
    cleaned = _ZERO_MEASUREMENT_RE.sub(" ", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([，,；;。！？!?])", r"\1", cleaned)
    return cleaned


def apply_identifier_numeric_fuse(
    answer: str,
    effective_knowledge: str,
    current_attachment_observation: Iterable[Any] | None = None,
    allowed_identifiers: Iterable[str] | None = None,
) -> GroundingResult:
    """Run the fuse against the visible answer, ignoring think blocks."""
    return evaluate_grounding(
        visible_answer_text(answer),
        effective_knowledge,
        current_attachment_observation,
        allowed_identifiers=allowed_identifiers,
    )
