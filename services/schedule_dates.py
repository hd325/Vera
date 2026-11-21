"""Annotate school-calendar dates with system-computed relative time.

Models misread spaced/interleaved table text (e.g. ``10 月19 日``) and bend
dates when asked for the "next" holiday. This module normalizes each month-day
date found in already-authorized evidence text and appends a deterministic
past/ongoing/upcoming marker so the model never does the date arithmetic.
It is storage-free and never changes any text other than the dates.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any, Mapping

import zhconv


_SEP = r"[~～\-－–—至到]"
_DATE = re.compile(
    r"(?<!\d)(?:(?P<y>20\d{2})\s*年\s*)?(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"
    r"(?:\s*" + _SEP + r"\s*(?:(?P<y2>20\d{2})\s*年\s*)?(?:(?P<m2>\d{1,2})\s*月\s*)?(?P<d2>\d{1,2})(?![\d:：])\s*日?)?"
)
_CN_DIGITS = str.maketrans("零〇一二三四五六七八九", "00123456789")
_ACADEMIC = re.compile(r"(20\d{2}|二[零〇]\S{2})\s*(?:[-/至~～－–]\s*(?:20)?\d{2}\s*)?学年")
_TERM_HEADING = re.compile(r"^第\s*([一二三四1234])\s*段\s*(?:测试|考试|测验|段测|段考)\s*[:：]")
_TERM_END = re.compile(r"^(?:\([一二三四五六七八九十]+\)|毕业(?:试|考试)\s*[:：])")
_TERM_NUMBERS = dict(zip("一二三四1234", (1, 2, 3, 4, 1, 2, 3, 4)))
_TEMPORAL = re.compile(
    r"下次|下一|最近|接下来|还有|多久|几天|什么时候|甚么时候|何时|哪天|几号|几月|今天|明天|后天|"
    r"本周|这周|下周|已经|过了|放假|假期|补假|开学|考试|测试|段考|统测|校历|行事历|活动"
)


def _normal(value: Any) -> str:
    return zhconv.convert(unicodedata.normalize("NFKC", str(value or "")), "zh-cn")


def wants_schedule_dates(question: str, route: Mapping[str, Any] | None) -> bool:
    """Only school/exam/time questions over local evidence get annotations."""
    if not route or not route.get("retrieve_local"):
        return False
    if route.get("reason") == "school_or_file_question":
        return True
    if (route.get("exam_intent") or {}).get("is_exam"):
        return True
    return bool(_TEMPORAL.search(_normal(question)))


def declared_academic_year(text: str) -> int | None:
    """Read the academic year printed in the school document."""
    match = _ACADEMIC.search(_normal(text))
    if match:
        value = match.group(1).translate(_CN_DIGITS)
        if value.isdigit() and len(value) == 4:
            return int(value)
    return None


def academic_start_year(text: str, today: date) -> int:
    """School year from an explicit label, else the current school year."""
    return declared_academic_year(text) or (today.year if today.month >= 8 else today.year - 1)


def term_date_excerpts(passages):
    """Group the numbered date entries in the calendar's exam section.

    Match headings in the document, keeping original passage IDs and verbatim
    excerpts, including date rows continued in the next passage or page.
    """
    groups = {}
    term = None
    in_exams = False
    for passage in passages:
        for line in passage["content"].splitlines():
            normalized = _normal(line).strip()
            if re.fullmatch(r"(?:\([一二三四五六七八九十]+\)\s*)?考试日期[:：]?", normalized):
                in_exams = True
                continue
            if not in_exams:
                continue
            if _TERM_END.match(normalized):
                term = None
                if normalized.startswith("("):
                    return groups
                continue
            if heading := _TERM_HEADING.match(normalized):
                term = _TERM_NUMBERS[heading[1]]
            if term is None:
                continue
            excerpts = groups.setdefault(term, [])
            if not excerpts or excerpts[-1]["chunk_id"] != passage["chunk_id"]:
                excerpts.append({**passage, "evidence_excerpt": line})
            else:
                excerpts[-1]["evidence_excerpt"] += "\n" + line
    return groups


def select_calendar_dates(index, records, terms, *, year, today, explicit=False):
    """Choose an authorized calendar with entries for the requested school year."""
    wanted_year = year or academic_start_year("", today)
    calendars = []
    for record in records.values():
        if not explicit and record.get("audience") != "school":
            continue
        passages = index.document_passages(record)
        year_hint = record.get("original_name") or record.get("name") or ""
        heading = "\n".join(item["content"] for item in passages[:3])
        if declared_academic_year(heading + "\n" + year_hint) != wanted_year:
            continue
        groups = term_date_excerpts(passages)
        selected = {term: groups[term] for term in terms
                    if term in groups and _DATE.search(" ".join(item["evidence_excerpt"] for item in groups[term]))}
        if selected:
            calendars.append((len(selected), record.get("published_at") or record.get("uploaded_at") or "", selected))
    if not calendars:
        return [], []
    selected = max(calendars, key=lambda entry: entry[:2])[2]
    # A passage may contain consecutive heading/continuation lines from more
    # than one requested term. Keep one citation and only those exact excerpts.
    chunks = {}
    for term in terms:
        for excerpt in selected.get(term, ()):
            chunk_id = excerpt["chunk_id"]
            if chunk_id in chunks:
                chunks[chunk_id]["evidence_excerpt"] += "\n" + excerpt["evidence_excerpt"]
            else:
                chunks[chunk_id] = {**excerpt, "calendar_academic_year": wanted_year}
    return list(chunks.values()), [term for term in terms if term not in selected]


def _status(start: date, end: date, today: date) -> str:
    if end < today:
        return "已過"
    if start <= today:
        return "今天" if start == end == today else "進行中"
    return f"未到，{(start - today).days}天後"


def annotate_schedule_dates(text: str, today: date, hint: str = "") -> str:
    """Rewrite each ``M月D日`` (or range) as ``M月D日〔YYYY年，狀態〕``.

    Explicit years win; otherwise months 8-12 belong to the school year's
    start and months 1-7 to the following calendar year.
    """
    if not text:
        return text
    start_year = academic_start_year(f"{hint}\n{text}", today)

    def year_for(month: int, explicit: str | None) -> int:
        if explicit:
            return int(explicit)
        return start_year if month >= 8 else start_year + 1

    def replace(match: re.Match) -> str:
        try:
            month, day = int(match["m"]), int(match["d"])
            start = date(year_for(month, match["y"]), month, day)
        except ValueError:
            return match.group(0)
        end = start
        if match["d2"]:
            end_month = int(match["m2"]) if match["m2"] else month
            end_year = (int(match["y2"]) if match["y2"]
                        else start.year if end_month >= month else start.year + 1)
            try:
                end = max(start, date(end_year, end_month, int(match["d2"])))
            except ValueError:
                end = start
        label = f"{start.month}月{start.day}日"
        if end != start:
            label += f"至{end.month}月{end.day}日"
        year = f"{start.year}年" if start.year == end.year else f"{start.year}–{end.year}年"
        return f"{label}〔{year}，{_status(start, end, today)}〕"

    return _DATE.sub(replace, text)
