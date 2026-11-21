"""Select one school examination from already-authorized published records.

This module is deliberately storage-free. Callers supply current records and
their extracted text, and remain responsible for access checks at the evidence
boundary. Index metadata and upload order are never used to combine exams.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import zhconv

from services.question_routing import parse_exam_intent
from services.review_policy import is_published_record


_MACAU = timezone(timedelta(hours=8))
_GRADE = re.compile(r"(?:高[一二三123]|初[一二三123]|小[一二三四五六123456])")
_DATE = re.compile(
    r"(?:(?P<year>20\d{2})\s*(?:年|[-/.])\s*)?"
    r"(?P<month>\d{1,2})\s*(?:月|[-/.])\s*(?P<day>\d{1,2})\s*日?"
)
_PERIODS = {"first", "second", "third", "fourth"}
_OUTLINE = re.compile(r"考[试纲]|测[试验].{0,6}(?:范围|规划|提纲)|统测.{0,6}(?:范围|提纲)|复习范围|命题规划")
_SUBJECT_LABELS = {
    "chinese": "語文", "math": "數學", "english": "英語", "physics": "物理",
    "chemistry": "化學", "biology": "生物", "civics": "公民", "history": "歷史",
    "geography": "地理", "computer": "電腦",
}


def _normal(value: Any) -> str:
    return zhconv.convert(unicodedata.normalize("NFKC", str(value or "")), "zh-cn").casefold().strip()


def _grade(value: Any) -> str:
    match = _GRADE.search(_normal(value))
    return match.group().translate(str.maketrans("123456", "一二三四五六")) if match else ""


def _header(text: str) -> str:
    """Read event identity before scope tables mentioning earlier examinations."""
    lines = []
    for line in _normal(text).splitlines():
        line = line.strip()
        if not line or re.fullmatch(r"---\s*第\s*\d+\s*页\s*---", line):
            continue
        if lines and (re.match(r"[二三四五六七八九十]\s*[.、:．]", line)
                      or re.match(r"(?:测试|考试|统测|测验)?范围", line)):
            break
        lines.append(line)
        if len(lines) >= 12:
            break
    return "\n".join(lines)[:1200]


def _event_type(header: str, name: str) -> str:
    # The document title takes precedence over incidental mentions of a
    # previous unified examination in its revision table or generated name.
    for text in (header, name):
        if re.search(r"小测|随堂测验", text):
            return "quiz"
        if re.search(r"统测|统一测[试验]", text):
            return "unified"
        if re.search(r"第[一二三四1234]段.{0,12}(?:测验|测试)|段(?:测|考|试)", text):
            return "regular"
        if "测验" in text:
            return "quiz"
        if re.search(r"段\s*(?:测试|测验|考试|考|测)|(?:测试|考试|测验)(?:命题|规划|时间|日期|范围|提纲)", text):
            return "regular"
    return ""


def _event_time_text(header: str) -> str:
    lines = header.splitlines()
    labeled = []
    for index, line in enumerate(lines):
        if re.search(r"(?:测试|考试|统测|段测|测验|小测)(?:时间|日期)", line):
            labeled.append(line)
            if index + 1 < len(lines) and not _DATE.search(line):
                labeled.append(lines[index + 1])
    # An issue/revision date is not another sitting when an explicit exam-time
    # field supplies the actual date, including OCR that wraps its value.
    return "\n".join(labeled) if labeled and _DATE.search("\n".join(labeled)) else header


def _event_dates(header: str, today: date) -> set[date]:
    # A school-year label supplies a year only when the date itself omits one.
    years = re.findall(r"(?<!\d)(20\d{2})\s*年", header)
    default_year = int(years[0]) if years else today.year
    result = set()
    for match in _DATE.finditer(_event_time_text(header)):
        try:
            result.add(date(int(match["year"] or default_year), int(match["month"]), int(match["day"])))
        except ValueError:
            continue
    return result


def _revision(record: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(record.get("published_at") or record.get("uploaded_at") or ""),
            str(record.get("uploaded_at") or ""), str(record.get("id") or ""))


def _event_detail(header: str) -> tuple[str, str]:
    """Keep demonstrably different same-day sittings separate from revisions."""
    time = re.search(r"(?<!\d)([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)", _event_time_text(header))
    start = f"{int(time[1]):02d}:{time[2]}" if time else ""
    tracks = set(re.findall(r"文科|理科", header))
    return start, next(iter(tracks)) if len(tracks) == 1 else ""


def _result(status: str, reason: str, records=(), clarification=None) -> dict[str, Any]:
    copies = [dict(record) for record in records]
    return {"status": status, "reason": reason, "records": copies,
            "document_ids": [record["id"] for record in copies], "clarification": clarification}


def select_exam_documents(
    question: str,
    records: Iterable[Mapping[str, Any]],
    texts: Mapping[str, str],
    *,
    user_profile: Mapping[str, Any] | None = None,
    now: datetime | date | None = None,
    intent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one event's newest outline, or an actionable clarification.

    Explicit subject, period, examination type, date and grade narrow the
    catalog first. Otherwise regular term exams precede unified tests, and the
    nearest upcoming date (or most recent past date) wins. Multiple undated
    events and competing subjects are not silently merged or guessed.
    """
    supplied_intent = intent is not None
    if intent is None:
        intent = parse_exam_intent(question)
    if not intent["is_exam"]:
        return _result("not_exam", "not_exam_question")
    if len(intent["dates"]) > 1 or len(intent["years"]) > 1:
        return _result("clarification", "multiple_requested_events", clarification="你提到了多個考試日期或年份，請指定要查的那一場考試。")
    today = (now.astimezone(_MACAU).date() if isinstance(now, datetime) and now.tzinfo
             else now.date() if isinstance(now, datetime)
             else now if isinstance(now, date) else datetime.now(_MACAU).date())
    profile = user_profile or {}
    grade_hint = intent.get("grade") if supplied_intent else question
    wanted_grade = _grade(grade_hint) or _grade(profile.get("grade") or profile.get("class"))
    candidates = []
    for record in records:
        document_id = record.get("id")
        if (not isinstance(document_id, str) or not document_id
                or not is_published_record(record) or record.get("deleted_at") or record.get("deleted")):
            continue
        text = texts.get(document_id, "")
        if not isinstance(text, str) or not text.strip():
            continue
        header = _header(text)
        name = _normal(record.get("name") or record.get("filename"))
        material = record.get("material_type")
        if material and material != "exam_outline":
            continue
        if not material and not _OUTLINE.search(header + "\n" + name):
            continue
        descriptive = parse_exam_intent(header + "\n" + name)
        subject = record.get("subject") or descriptive["subject"]
        period = record.get("study_period")
        period = period if period in _PERIODS else descriptive["study_period"]
        record_grade = _grade(record.get("audience_grade") or record.get("grade")) or _grade(header)
        if wanted_grade and record_grade and wanted_grade != record_grade:
            continue
        if intent["subject"] and subject != intent["subject"]:
            continue
        if intent["study_period"] and period != intent["study_period"]:
            continue
        event_type = _event_type(header, name)
        if intent["exam_type"] and event_type != intent["exam_type"]:
            continue
        dates = _event_dates(header, today)
        if intent["dates"]:
            dates = {value for value in dates if (value.month, value.day) in intent["dates"]}
            if not dates:
                continue
        if intent["years"]:
            dates = {value for value in dates if value.year in intent["years"]}
            if not dates:
                continue
        candidates.append({"record": record, "subject": subject, "period": period,
                           "grade": record_grade, "type": event_type, "dates": dates,
                           "detail": _event_detail(header)})
    if not candidates:
        return _result("no_match", "no_matching_outline")
    subjects = {item["subject"] for item in candidates}
    if len(subjects) > 1:
        labels = "、".join(_SUBJECT_LABELS.get(value, value or "未標科目") for value in sorted(subjects))
        return _result("clarification", "multiple_subjects", clarification=f"你想查哪一科的考試範圍？目前有{labels}的提綱。")
    if not intent["exam_type"] and not intent["dates"]:
        # Explicit dates may intentionally point at a unified test; do not
        # replace that event merely because another regular exam exists.
        for preferred in ("regular", "unified", "quiz"):
            selected_type = [item for item in candidates if item["type"] == preferred]
            if selected_type:
                candidates = selected_type
                break
    if any(len(item["dates"]) > 1 for item in candidates):
        return _result("clarification", "multiple_dates_in_outline", clarification="提綱中出現多個考試日期，請指定你要查的考試日期。")
    if len(candidates) == 1:
        return _result("matched", "single_matching_outline", [candidates[0]["record"]])
    if any(not item["dates"] for item in candidates):
        return _result("clarification", "undated_competing_outlines", clarification="找到多份符合的考試提綱，但日期資料不足，請指定考試日期或檔案。")
    future = {next(iter(item["dates"])) for item in candidates if next(iter(item["dates"])) >= today}
    target = min(future) if future else max(next(iter(item["dates"])) for item in candidates)
    finalists = [item for item in candidates if target in item["dates"]]
    identities = {(item["subject"], item["period"], item["grade"], item["type"], item["detail"]) for item in finalists}
    if len(identities) != 1:
        return _result("clarification", "competing_exam_events", clarification=f"{target.month}月{target.day}日有多份不同場次的提綱，請指定段次或考試類型。")
    chosen = max(finalists, key=lambda item: _revision(item["record"]))
    return _result("matched", "nearest_exam_latest_revision", [chosen["record"]])
