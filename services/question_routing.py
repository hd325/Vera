"""Small, deterministic decisions about when school-file evidence is needed.

This module does not query storage, score providers, or grant permissions. The
caller supplies the current owner's history and intersects any document scope
with its existing permission filter before retrieval.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping

import zhconv


_FILE_TAG = re.compile(r"\[([^\[\]\n]+\.(?:pdf|docx?|txt|png|jpe?g|gif|xlsx?|pptx?|md|csv|zip|rar|7z))\]", re.I)
_PAGE = re.compile(r"第\s*(\d+)\s*[页頁]|\bp(?:age)?\.?\s*(\d+)\b", re.I)
_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:月|[/.-])\s*(\d{1,2})\s*[日号號]?(?!\d)")
_SCHOOL = re.compile(
    r"校历|校内|本校|我校|学校|校园|濠江|校长|校训|校区|行政简历|班级|班会|放假|补假|假期|寒假|开学|校运会|"
    r"毕业旅行|毕业班|毕业礼|结业礼|研学|春游|秋游|旅行团|行程表|行程安排|活动安排|报名指南|参赛指南|服务费|一览表"
)
_LOCAL_DOC = re.compile(
    r"行程表|行程安排|毕业旅行|一览表|服务费|报名指南|参赛指南|报名表|时间表|课程表|课表|节目单|自荐信|"
    r"理解性默写|古代文化常识|中国现当代文学|外国文学|"
    r"(?:你有|有没有|找到|查一下|查看|给我看).{0,15}(?:表|指南|通知|提纲|大纲|讲义|课件|试卷|卷子|练习|作业|自荐信|名单|手册|方案|报告|默写)"
)
_EXAM = re.compile(r"考试(?:安排|范围|时间|日期|通知|提纲)|考纲|考核范围|复习范围")
_FILE_TASK = re.compile(r"(?:这|那|该|上述|上面|之前|上传|附件|引用|提供).{0,5}(?:文档|文件|档案|资料|讲义|课件|试卷)|(?:文件|档案|文档|讲义|课件|校历|试卷|练习册)(?:里|中|内容|讲|写|提|第)|根据.{0,20}(?:文件|档案|资料|讲义|课件)|总结.{0,5}(?:文件|档案|文档)")
_FOLLOWUP = re.compile(r"^(?:那|那么|还有|接着|继续|这|该|上面|刚才|之前|再)|(?:第\s*\d+\s*页|这份文件|这份档案|那份文件|这道题|下一页)")
_SHORT_FOLLOWUP = re.compile(r"^(?:再|继续|接着)?(?:解释|说明|讲|说).{0,8}(?:详细|清楚|一点|一下|下去)[。？！?!]*$")
_SCHEDULE_DOC = re.compile(
    r"校历|行政简历|行事历|校内|学校|校园|学年.{0,8}安排|开学|放假|补假|假期|寒假|校运会|班会|活动安排|"
    r"毕业旅行|毕业班|毕业礼|结业礼|研学|春游|秋游|旅行团|行程表|行程安排|报名指南|参赛指南|服务费|一览表"
)
# 质疑/核对上一条答案：只在上一条回答实际引用过文件时继承该文件重新检索。
_VERIFY_FOLLOWUP = re.compile(
    r"你确定|确定吗|确定是|真的吗|真的是|对吗|对不对|没错吗|(?:搞|弄|写|看|记|算)错|错了|不对|"
    r"核对|核实|再查|再确认|重新(?:查|看|核)|不是.{0,30}(?:吗|吧|么)"
)
_EXAM_DOC = re.compile(r"考试|统测|测验|小测|测试|段考|考纲|考核|命题|复习范围")
_SUBJECT_ALIASES = {
    "math": ("数学", "math", "mathematics"),
    "chinese": ("语文", "中文", "chinese"),
    "english": ("英语", "英文", "english"),
    "physics": ("物理", "physics"),
    "chemistry": ("化学", "chemistry"),
    "biology": ("生物", "biology"),
    "history": ("历史", "history"),
    "geography": ("地理", "geography"),
    "civics": ("公民", "civics"),
    "computer": ("电脑", "计算机", "资讯科技", "computer"),
}
_PERIOD = re.compile(r"(?<![一二三四五六七八九十\d])(?:第\s*)?([一二三四1-4])\s*段")
_EN_PERIOD_PATTERNS = (
    ("first", re.compile(r"\b(?:first|1st)\s+(?:period|term|segment|stage)\b")),
    ("second", re.compile(r"\b(?:second|2nd)\s+(?:period|term|segment|stage)\b")),
    ("third", re.compile(r"\b(?:third|3rd)\s+(?:period|term|segment|stage)\b")),
    ("fourth", re.compile(r"\b(?:fourth|4th)\s+(?:period|term|segment|stage)\b")),
)
_PERIOD_CODES = {"一": "first", "1": "first", "二": "second", "2": "second",
                 "三": "third", "3": "third", "四": "fourth", "4": "fourth"}
_EXAM_NOUN = re.compile(r"考试|段考|统测|测验|小测|测试|段测")
_EXAM_TIME = re.compile(r"什么时候|何时|几时|几点|几号|哪天|哪一天|几月|时间|日期")
_EXAM_RANGE = re.compile(r"考(?:试)?(?:的|些|了|的是)?\s*(?:什么|甚么|哪些|哪几|哪部分|哪章|几章)|考(?:试)?范围|考(?:试)?内容|题型|配分|分值|占比")
_EXAM_BACKGROUND = re.compile(r"焦虑|紧张|害怕|失眠|心慌|压力|怎么复习|如何复习|怎么准备|如何准备|是什么|是什么意思|定义")
_NO_DOCUMENT = "__no_authorized_document__"

_BILINGUAL_CONCEPTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("毕业旅行", "北京七天", "爱国之旅"), ("graduation trip", "beijing trip", "school trip")),
    (("行程表", "行程安排"), ("itinerary", "trip schedule", "travel schedule")),
    (("校历", "行事历", "行政简历"), ("school calendar", "academic calendar")),
    (("智能手机",), ("smartphone", "smartphone-induced", "smartphones")),
    (("理解性默写", "名句默写", "古诗文默写"), ("comprehension dictation", "poem dictation", "dictation")),
    (("古代文化常识", "文化常识"), ("ancient chinese culture", "cultural knowledge", "ancient culture")),
    (("中国现当代文学", "现当代文学"), ("modern chinese literature", "contemporary chinese literature")),
    (("外国文学",), ("foreign literature", "world literature")),
    (("选择性服务费", "服务费", "一览表"), ("optional service fee", "service fee", "fee schedule")),
    (("自荐信",), ("cover letter", "self-recommendation letter", "application letter")),
    (("参赛指南", "报名指南", "ai创新赛"), ("competition guide", "registration guide", "ai competition")),
    (("考试范围", "考试提纲", "考核范围", "复习范围"), ("exam scope", "exam syllabus", "exam outline", "test scope")),
    (("统测",), ("unified test", "unified exam")),
    (("小测",), ("quiz",)),
    (("电磁感应", "楞次定律", "法拉第"), ("electromagnetic induction", "lenz's law", "lenz law", "faraday")),
    (("有机化学", "烯烃", "炔烃", "原子利用率"), ("organic chemistry", "alkene", "alkyne", "atom economy")),
)
_EN_QUERY_STOPWORDS = frozenset({
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
    "that", "this", "these", "those", "with", "from", "have", "has", "had",
    "does", "did", "done", "about", "into", "unto", "your", "their", "there",
    "here", "would", "could", "should", "shall", "will", "might", "must",
    "explain", "please", "tell", "give", "show", "find", "list", "describe",
    "question", "questions", "answer", "answers", "file", "files", "document",
    "documents", "original", "page", "pages", "according", "some", "many",
    "much", "more", "most", "other", "another", "such", "only", "also", "very",
})


def _normal(text: Any) -> str:
    return zhconv.convert(unicodedata.normalize("NFKC", str(text or "")), "zh-cn").casefold().strip()


def expand_bilingual_query(text: Any) -> str:
    """Bridge Simplified/Traditional Chinese and English school/course concepts."""
    base = _normal(text)
    if not base:
        return ""
    extras: list[str] = []
    for zh_terms, en_terms in _BILINGUAL_CONCEPTS:
        zh_hit = any(term in base for term in zh_terms)
        en_hit = any(
            (re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", base) if term.isascii() else term in base)
            for term in en_terms
        )
        if zh_hit and not en_hit:
            extras.extend(en_terms)
        elif en_hit and not zh_hit:
            extras.extend(zh_terms)
    if not extras:
        return base
    return base + " " + " ".join(dict.fromkeys(extras))


def _filename(text: Any) -> str:
    return _normal(text).replace("\\", "/").rsplit("/", 1)[-1].strip("[] \t\r\n\"'")


def _subjects_in(text: Any) -> set[str]:
    normalized = expand_bilingual_query(text)
    return {code for code, aliases in _SUBJECT_ALIASES.items()
            if any((re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", normalized)
                    if alias.isascii() else alias in normalized) for alias in aliases)}


def _periods_in(text: Any) -> set[str]:
    normalized = _normal(text)
    periods = {_PERIOD_CODES[value] for value in _PERIOD.findall(normalized)}
    for code, pattern in _EN_PERIOD_PATTERNS:
        if pattern.search(normalized):
            periods.add(code)
    return periods


def parse_exam_intent(question: str) -> dict[str, Any]:
    """Parse school-exam facts without treating study advice as file requests.

    Subject and period values use the existing upload catalog codes. They are
    returned even for a document title or a non-exam question so callers can
    compare catalog metadata without running a model or searching storage.
    Bracketed file names describe the selected source, not the user's intent.
    """
    raw_q = _normal(_FILE_TAG.sub("", str(question or "")))
    q = expand_bilingual_query(raw_q)
    subjects = _subjects_in(q)
    periods = _periods_in(q)
    noun = bool(_EXAM_NOUN.search(q))
    fact_query = bool(_EXAM_RANGE.search(q) or _EXAM_TIME.search(q)
                      or re.search(r"安排|通知|提纲|考纲|考核范围|复习范围", q))
    local_anchor = bool(subjects or periods or _SCHOOL.search(q)
                        or re.search(r"这次|本次|下次|期中|期末|段考|统测|测验|小测", q))
    is_exam = bool(_EXAM.search(q) or
                   (local_anchor and fact_query and re.search(r"考|测", q)) or
                   (local_anchor and noun and not _EXAM_BACKGROUND.search(raw_q)
                    and not re.search(r"为什么|如何|怎么|什么|甚么", raw_q)))
    if _EXAM_BACKGROUND.search(raw_q) and not (_EXAM.search(q) or _EXAM_RANGE.search(q)):
        is_exam = False
    regular_exam = bool(re.search(r"段考|段测|段试|期[中末]考试", q)
                        or (periods and re.search(_PERIOD.pattern + r".{0,8}(?:测试|测验|考试)", q)))
    exam_type = ("unified" if "统测" in q else
                 "quiz" if re.search(r"小测|随堂测验", q) else
                 "regular" if regular_exam else
                 "quiz" if "测验" in q else "")
    return {
        "is_exam": is_exam,
        "subject": next(iter(subjects)) if len(subjects) == 1 else "",
        "study_period": next(iter(periods)) if len(periods) == 1 else "",
        "exam_type": exam_type,
        "dates": sorted(_dates(raw_q)),
        "years": sorted({int(value) for value in re.findall(r"(?<!\d)(20\d{2})(?:学年|年度|年|[-/])", raw_q)}),
    }


def parse_file_references(question: str, file_references: str = "") -> list[str]:
    """Extract explicit file names without permissive substring matching."""
    values = [part.strip().strip("[]") for part in re.split(r"[\r\n]+", file_references or "") if part.strip()]
    values.extend(match.group(1).strip() for match in _FILE_TAG.finditer(question or ""))
    return list(dict.fromkeys(values))


def answer_file_citation_numbers(text: str) -> list[int]:
    """Only answer prose can establish a used file; thoughts/code are examples."""
    answer = re.sub(r"<think>[\s\S]*?(?:</think>|$)", "", str(text or ""), flags=re.I)
    answer = re.sub(r"(```|~~~)[\s\S]*?\1|`[^`\n]*`", "", answer)
    return [int(value) for value in re.findall(r"\[(\d+)\]", answer)]


def _previous_file_scope(messages: Iterable[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Only the immediately preceding exchange can supply an implicit scope.

    An assistant's candidate citation_map alone is insufficient: its answer
    must actually contain the corresponding numeric marker. Stopping at the
    preceding user turn prevents a later general topic reviving an old file.
    """
    for message in reversed(list(messages)):
        if message.get("is_user"):
            names = parse_file_references(str(message.get("content", "")), str(message.get("file_references", "")))
            return names, []
        citations = message.get("citation_map") or []
        used = {str(number) for number in answer_file_citation_numbers(message.get("content", ""))}
        ids = list(dict.fromkeys(str(item.get("document_id")) for index, item in enumerate(citations, 1)
                                 if str(item.get("index") or index) in used and item.get("document_id")))
        if ids:
            return [], ids
    return [], []


def route_question(question: str, file_references: str = "", recent_messages=None, *,
                   conversation_owner=None, user_id=None, skill_id="none") -> dict[str, Any]:
    """Route generic knowledge separately from file/school-specific questions."""
    q = _normal(question)
    q_expanded = expand_bilingual_query(q)
    names = parse_file_references(question, file_references)
    exam_intent = parse_exam_intent(question)
    route = {"kind": "general", "retrieve_local": False, "reason": "general_knowledge",
             "file_names": names, "document_ids": [], "inherited": False,
             "exam_intent": exam_intent,
             "simple_fact": bool(len(q) <= 80 and re.search(r"什么|哪天|几号|哪年|谁|多少|what|when|who", q)
                                 and not re.search(r"解释|分析|推导|证明|比较|详细|计算|为什么|如何|怎么", q)
                                 and skill_id in (None, "", "none"))}
    if names:
        route.update(kind="file", retrieve_local=True, reason="explicit_file")
        return route
    follows_file = bool(_SHORT_FOLLOWUP.fullmatch(q) or _VERIFY_FOLLOWUP.search(q) or
                        (_FOLLOWUP.search(q) and (_PAGE.search(q) or _FILE_TASK.search(q) or re.search(r"这道题|下一页", q))))
    if follows_file and conversation_owner is not None and conversation_owner == user_id:
        inherited_names, inherited_ids = _previous_file_scope(recent_messages or [])
        if inherited_names or inherited_ids:
            route.update(kind="file", retrieve_local=True, reason="file_followup", inherited=True,
                         file_names=inherited_names, document_ids=inherited_ids)
            return route
    if (_PAGE.search(q) or _FILE_TASK.search(q)) and not (_SCHOOL.search(q_expanded) or _LOCAL_DOC.search(q_expanded) or exam_intent["is_exam"]):
        route.update(kind="file", retrieve_local=True, reason="missing_file_scope", simple_fact=False)
        return route
    if _SCHOOL.search(q_expanded) or _LOCAL_DOC.search(q_expanded) or exam_intent["is_exam"]:
        route.update(kind="local", retrieve_local=True, reason="school_or_file_question", simple_fact=False)
    return route


_DOC_KEYWORDS = (
    "毕业旅行", "行程表", "行程安排", "行政简历", "行事历", "校历",
    "一览表", "服务费", "报名指南", "参赛指南", "报名表", "时间表",
    "课程表", "课表", "节目单", "自荐信", "校运会", "毕业礼", "结业礼",
    "理解性默写", "古代文化常识", "中国现当代文学", "外国文学", "智能手机",
)
_GENERIC_TITLE_STOPWORDS = frozenset({
    "学校", "澳门", "濠江", "中学", "年度", "学年", "文件", "资料", "附件",
    "其他", "讲义", "课件", "练习", "试卷", "测试", "测验", "考试", "通知",
    "个人", "年级", "班级", "复习卷子", "课堂课件", "考试提纲", "课后作业",
    "学生版", "教师版", "汇总", "第一批", "第二批",
    "高一", "高二", "高三", "初一", "初二", "初三",
    "小一", "小二", "小三", "小四", "小五", "小六",
    "第一段", "第二段", "第三段", "第四段",
})
_SECTION_HEADING_RE = re.compile(
    r"(?:^|\n)\s*(?:[一二三四五六七八九十]+[、.．]\s*|[(（][一二三四五六七八九十\d]+[)）]\s*|"
    r"第[一二三四五六七八九十\d]+[章节節部分篇]\s*|§\s*\d+(?:\.\d+)*\s*)([^\n（(【]{2,28})"
)
_BOOK_TITLE_RE = re.compile(r"《([^》\n]{2,24})》")
_ALNUM_CODE_RE = re.compile(
    r"(?<![a-z0-9])(?:[a-z]+[0-9][a-z0-9_-]*|[0-9]+[a-z][a-z0-9_-]*)(?![a-z0-9])"
)
_STEM_PREFIX_RE = re.compile(
    r"^(?:(?:个人|年级|班级|学校|高[一二三1-3](?:[一二三四五六七八九十\d]+班)?|初[一二三1-3]|小[一二三四五六1-6]|"
    r"语文|数学|英语|英文|物理|化学|生物|历史|地理|公民|电脑|计算机|其他|"
    r"第一段|第二段|第三段|第四段|小测|统测|复习卷子|统测考试卷|考试提纲|课堂课件|课后作业|"
    r"20\d{2}(?:[-~—]20\d{2})?(?:学年|年度)?)[_\-\s]*)+"
)


def quantify_file_record(record: Mapping[str, Any], content: str = "") -> dict[str, Any]:
    """Quantify a file record's multi-dimensional search features (titles, headings, codes, bilingual tags)."""
    raw_fields = [
        str(record.get(field) or "")
        for field in ("name", "original_filename", "original_name", "material_custom", "subject_custom", "notes")
        if record.get(field)
    ]
    extra_kw = record.get("search_keywords") or record.get("topic_tags") or ()
    if isinstance(extra_kw, str):
        raw_fields.append(extra_kw)
    elif isinstance(extra_kw, (list, tuple, set)):
        raw_fields.extend(str(item) for item in extra_kw if item)

    title_norm = expand_bilingual_query(" ".join(_normal(val) for val in raw_fields))
    stems: set[str] = set()
    for field in ("material_custom", "subject_custom", "name", "original_name", "original_filename", "notes"):
        raw = _filename(record.get(field, ""))
        if not raw:
            continue
        stem = re.sub(r"\.[a-z0-9]{1,8}$", "", raw)
        stem = re.sub(r"[（(](?:学生版|教师版|汇总|给学校确认|\d+)[)）]", "", stem)
        cleaned = _STEM_PREFIX_RE.sub("", stem)
        for part in [cleaned, *re.split(r"[_\-\s（()）~—]+", stem)]:
            part_clean = _STEM_PREFIX_RE.sub("", part)
            compact = re.sub(r"[^\w\u3400-\u9fff]+", "", part_clean)
            if len(compact) >= 2 and compact not in _GENERIC_TITLE_STOPWORDS:
                stems.add(compact)

    norm_content = _normal(content) if content else ""
    headings: set[str] = set()
    book_titles: set[str] = set()
    if norm_content:
        for match in _SECTION_HEADING_RE.finditer(norm_content):
            heading = re.sub(r"[^\w\u3400-\u9fff]+", "", match.group(1))
            if 2 <= len(heading) <= 24 and heading not in _GENERIC_TITLE_STOPWORDS:
                headings.add(heading)
        for match in _BOOK_TITLE_RE.finditer(norm_content):
            bt = re.sub(r"[^\w\u3400-\u9fff]+", "", match.group(1))
            if len(bt) >= 2:
                book_titles.add(bt)
        for page_match in re.finditer(r"---\s*第\s*\d+\s*页\s*---\s*\n([^\n]{3,60})", norm_content):
            line = re.sub(r"[^\w\u3400-\u9fff\s-]+", " ", page_match.group(1)).strip()
            compact_line = re.sub(r"[^\w\u3400-\u9fff]+", "", line)
            if 3 <= len(compact_line) <= 28 and compact_line not in _GENERIC_TITLE_STOPWORDS:
                headings.add(compact_line)

    codes = set(_ALNUM_CODE_RE.findall(title_norm + "\n" + norm_content))
    en_words = {
        w for w in re.findall(r"[a-z]{4,}", title_norm + "\n" + norm_content)
        if w not in _EN_QUERY_STOPWORDS
    }
    return {
        "title_norm": title_norm,
        "stems": stems,
        "headings": headings,
        "book_titles": book_titles,
        "codes": codes,
        "en_words": en_words,
        "content_norm": norm_content,
    }


def _extract_query_cjk_windows(q_raw: str) -> list[str]:
    """Extract distinctive 5-char CJK phrases from a query for line-level matching."""
    cleaned = re.sub(
        r"(?:请问|告诉|帮我|查一下|文件里|档案里|资料里|课件里|讲义里|卷子里|里面|当中|其中|"
        r"关于|根据|按照|是什么|什么意思|有哪些|有多少|是哪些|为什么|怎么|如何|"
        r"第[一二三四五六七八九十\d]+[段页题章]|这道题|这份文件|那份文件)",
        " ",
        q_raw,
    )
    windows: list[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]{5,}", cleaned):
        seg_trimmed = re.sub(r"^[的之在中和与及或把将从向対对给]|[的吗呢吧呀啊了么]$", "", seg)
        if len(seg_trimmed) >= 5:
            for i in range(len(seg_trimmed) - 4):
                windows.append(seg_trimmed[i:i + 5])
    return list(dict.fromkeys(windows))


def score_chunk_relevance(question: str, chunk: Mapping[str, Any]) -> float:
    """Quantify how well a single chunk answers the user's question (phrase, line, n-gram & bilingual)."""
    q_raw = _normal(_FILE_TAG.sub("", str(question or "")))
    if not q_raw:
        return 0.0
    q = expand_bilingual_query(q_raw)
    content = _normal(chunk.get("content", ""))
    filename = _normal(chunk.get("filename", ""))
    if not content:
        return 0.0

    score = 0.0
    for kw in _DOC_KEYWORDS:
        if kw in q and (kw in content or kw in filename):
            score += 12.0
    for bt in _BOOK_TITLE_RE.findall(q_raw):
        bt_clean = re.sub(r"[^\w\u3400-\u9fff]+", "", bt)
        if bt_clean and (f"《{bt_clean}》" in content or bt_clean in content):
            score += 18.0
    for code in _ALNUM_CODE_RE.findall(q_raw):
        if code in content or code in filename:
            score += 15.0

    windows_5 = _extract_query_cjk_windows(q_raw)
    for win in windows_5:
        if win in content:
            score += 10.0

    cjk_chars = "".join(re.findall(r"[\u4e00-\u9fff]", q))
    grams_3 = {cjk_chars[i:i + 3] for i in range(len(cjk_chars) - 2)} if len(cjk_chars) >= 3 else set()
    grams_2 = {cjk_chars[i:i + 2] for i in range(len(cjk_chars) - 1)} if len(cjk_chars) >= 2 else set()
    for g3 in grams_3:
        if g3 in content:
            score += 2.0
    for g2 in grams_2:
        if g2 in content:
            score += 0.5

    if grams_3:
        best_line_hits = max(
            (sum(1 for g3 in grams_3 if g3 in line) for line in content.splitlines() if line.strip()),
            default=0,
        )
        if best_line_hits >= 2:
            score += best_line_hits * 3.5

    q_en = {w for w in re.findall(r"[a-z]{4,}", q) if w not in _EN_QUERY_STOPWORDS}
    for word in q_en:
        if word in content or word in filename:
            score += 3.0
    return score


def select_matching_local_files(
    question: str,
    file_records: Iterable[Mapping[str, Any]],
    file_contents: Mapping[str, str] | None = None,
) -> list[str]:
    """Identify and rank authorized records whose title, section headings, bilingual concepts, or lines match."""
    q_raw = _normal(_FILE_TAG.sub("", str(question or "")))
    if not q_raw:
        return []
    if re.search(r"焦虑|紧张|害怕|失眠|心慌|压力|怎么复习|如何复习|怎么准备|如何准备", q_raw):
        return []
    q = expand_bilingual_query(q_raw)
    if _dates(q_raw) and not (_SCHOOL.search(q) or _LOCAL_DOC.search(q) or _EXAM_NOUN.search(q)):
        return []

    q_compact = re.sub(r"[^\w\u3400-\u9fff]+|(?:那[份个]|这[份个]|的|之)", "", q)
    q_books = {re.sub(r"[^\w\u3400-\u9fff]+", "", m) for m in _BOOK_TITLE_RE.findall(q_raw) if len(m) >= 2}
    q_codes = set(_ALNUM_CODE_RE.findall(q_raw)) - {"2024", "2025", "2026", "2027"}
    q_en = {w for w in re.findall(r"[a-z]{4,}", q) if w not in _EN_QUERY_STOPWORDS}
    q_windows = _extract_query_cjk_windows(q_raw)

    scored: list[tuple[float, int, str]] = []
    for idx, record in enumerate(file_records):
        fid = str(record.get("id") or "")
        if not fid:
            continue
        raw_content = (file_contents or {}).get(fid, "") if file_contents else ""
        features = quantify_file_record(record, raw_content)
        score = 0.0

        for kw in _DOC_KEYWORDS:
            if kw in q and (kw in features["title_norm"] or kw in features["headings"]):
                score += 8.0

        for stem in features["stems"]:
            if len(stem) >= 3 and stem in q_compact:
                score += 9.0
            elif len(q_compact) >= 4 and q_compact in stem:
                score += 7.0

        for heading in features["headings"]:
            if len(heading) >= 3 and heading in q_compact:
                score += 7.5

        if q_books and q_books.intersection(features["book_titles"]):
            score += 8.5

        if q_codes and q_codes.intersection(features["codes"]):
            score += 8.0

        if q_en:
            en_overlap = q_en.intersection(features["en_words"])
            if len(en_overlap) >= 2 or any(len(w) >= 7 for w in en_overlap):
                score += 5.0 + len(en_overlap) * 1.5

        if q_windows and features["content_norm"]:
            win_hits = sum(1 for win in q_windows if win in features["content_norm"])
            if win_hits >= 1:
                score += 4.5 + min(win_hits, 5) * 1.5

        if score > 0:
            if not record.get("file_task_id"):
                score += 0.2
            scored.append((score, -idx, fid))

    scored.sort(reverse=True)
    if not scored:
        return []
    top_score = scored[0][0]
    cutoff = top_score * 0.45 if top_score >= 12.0 else 0.0
    return [fid for score, _, fid in scored if score >= cutoff]


def restrict_document_filter(permission_filter: Mapping[str, Any], route: Mapping[str, Any],
                             file_records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Narrow an existing allowed-ID filter; never turn a name into permission."""
    result = dict(permission_filter)
    if route.get("kind") != "file":
        return result
    allowed = set((permission_filter.get("document_id") or {}).get("$in") or [])
    wanted = {_filename(value) for value in route.get("file_names", [])}
    selected = set(str(value) for value in route.get("document_ids", []) if str(value) in allowed)
    if wanted:
        selected.update(str(record.get("id")) for record in file_records
                        if record.get("id") in allowed and _filename(record.get("name") or record.get("filename")) in wanted)
    result["document_id"] = {"$in": sorted(selected & allowed) or [_NO_DOCUMENT]}
    return result


def _dates(text: str) -> set[tuple[int, int]]:
    return {(int(month), int(day)) for month, day in _DATE.findall(text)
            if 1 <= int(month) <= 12 and 1 <= int(day) <= 31}


def filter_local_evidence(question: str, candidates: Iterable[Mapping[str, Any]], route: Mapping[str, Any]):
    """Reject explainably mismatched dates, pages, document types and entities.

    Hybrid RRF/BM25 scores are ranks rather than probabilities; they are never
    used as a confidence cutoff here. Rejected candidates are not backfilled.
    Internal verified exam IDs mean the caller has checked the full document's
    identity/date, so continuation chunks need not repeat its header. This does
    not authorize access, and explicit page constraints always remain strict.
    """
    q_raw = _normal(_FILE_TAG.sub("", question))
    q = expand_bilingual_query(q_raw)
    exam_intent = parse_exam_intent(question)
    wanted_dates = _dates(q_raw)
    wanted_years = set(re.findall(r"(?<!\d)(20\d{2})(?:年|[-/])", q_raw))
    pages = {int(a or b) for a, b in _PAGE.findall(q_raw)}
    subjects = _subjects_in(q)
    periods = _periods_in(q) if exam_intent["is_exam"] else set()
    entities = {_normal(value) for value in re.findall(r"[「『“\"]([^」』”\"]{2,40})[」』”\"]", question)}
    entities.update(_ALNUM_CODE_RE.findall(q_raw))
    entities -= {"pdf", "docx", "txt", "pptx", "xlsx", "page", "the", "what", "when", "who", "1st", "2nd", "3rd", "4th"}
    entities -= {alias for aliases in _SUBJECT_ALIASES.values() for alias in aliases if alias.isascii()}
    check_subject = bool(subjects and (route.get("kind") != "file" or exam_intent["is_exam"]))
    verified_exam_ids = set(route.get("verified_exam_document_ids") or ())
    accepted, rejected = [], []
    for candidate in candidates:
        item = dict(candidate)
        name = _normal(item.get("filename", ""))
        content = _normal(item.get("content", ""))
        evidence = expand_bilingual_query(name + "\n" + content)
        metadata = item.get("metadata") or {}
        metadata_subject = _normal(item.get("subject") or metadata.get("subject"))
        document_subjects = _subjects_in(name) | _subjects_in(metadata_subject)
        if not document_subjects:
            document_subjects = _subjects_in(content)
        # File titles are more specific than month-derived upload periods.
        document_periods = _periods_in(name)
        if not document_periods:
            metadata_period = _normal(item.get("study_period") or metadata.get("study_period"))
            document_periods = ({metadata_period} if metadata_period in _PERIOD_CODES.values()
                                else _periods_in(metadata_period))
        if not document_periods:
            document_periods = _periods_in(content)
        material_type = item.get("material_type") or metadata.get("material_type")
        exam_document = bool(_EXAM_DOC.search(evidence) or
                             material_type in {"exam_outline", "exam_paper", "unified_test", "quiz"})
        verified_exam = item.get("document_id") in verified_exam_ids
        reason = None
        if pages and str(item.get("page")) not in {str(page) for page in pages}:
            reason = "page_mismatch"
        elif not verified_exam and wanted_dates and not wanted_dates.issubset(_dates(content)):
            reason = "date_not_in_fragment"
        elif not verified_exam and wanted_years and not all(year in evidence for year in wanted_years):
            reason = "year_mismatch"
        elif not verified_exam and check_subject and not subjects.intersection(document_subjects):
            reason = "subject_mismatch"
        elif not verified_exam and periods and document_periods and not periods.intersection(document_periods):
            reason = "study_period_mismatch"
        elif entities and not all(entity in evidence for entity in entities):
            reason = "entity_mismatch"
        elif route.get("kind") != "file":
            if exam_intent["is_exam"] and not verified_exam and not exam_document:
                reason = "exam_document_mismatch"
            elif not exam_intent["is_exam"] and _SCHOOL.search(q) and not _SCHEDULE_DOC.search(evidence) and not any(word in evidence and word in q for word in ("校长", "校训", "校区", "濠江", "班级")):
                reason = "school_document_mismatch"
            elif _FILE_TASK.search(q) and not (_SCHOOL.search(q) or exam_intent["is_exam"]):
                # A deictic file request with no owner-scoped antecedent must
                # not accidentally summarize an arbitrary accessible file.
                reason = "missing_file_scope"
        if reason:
            rejected.append({"chunk_id": item.get("chunk_id"), "reason": reason})
        else:
            accepted.append(item)
    return accepted, rejected
