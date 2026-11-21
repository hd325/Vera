import unittest
from datetime import date
from types import SimpleNamespace

import tests  # Establish synthetic-only storage before application imports.
from services.schedule_dates import annotate_schedule_dates, select_calendar_dates
from services.turn_planner import normalize_plan


class CalendarEvidenceTests(unittest.TestCase):
    def test_official_calendar_uses_matching_sections_and_retains_citation_anchors(self):
        original = [
            "二零二六學年上課、放學、假期、考試以及結業時間",
            "(三) 考試日期\n第一段測試：中學11 月9 日－12 日。",
            "第二段考試：中學2027 年1 月25 日－28 日。\n第三段測試：高三級2027 年3 月30 日－4 月2 日；",
            "中學(高三級除外) 2027年4月19日－22日；小學2027年4月20日－22日；",
            "幼稚園2027 年4 月21－22 日。\n畢業試：高三級 2027 年5月14日。",
            "第四段考試：中學6月28日－7月1日。\n(四) 2027年2月1日放寒假。",
        ]
        records = {
            "official": {"id": "official", "name": "第一段_校曆.pdf", "audience": "school"},
            "summary": {"id": "summary", "name": "2026學年校曆_整理版.docx", "audience": "personal"},
        }
        index = SimpleNamespace(document_passages=lambda record: [
            {"chunk_id": f"{record['id']}-{i}", "content": text}
            for i, text in enumerate(original[:3] if record["id"] == "summary" else original)
        ])
        today = date(2026, 11, 18)
        chosen, missing = select_calendar_dates(index, records, [3], year=None, today=today)
        self.assertEqual(missing, [])
        self.assertEqual([item["chunk_id"] for item in chosen], ["official-2", "official-3", "official-4"])
        self.assertIn("畢業試", chosen[-1]["content"])
        dates = "\n".join(item["evidence_excerpt"] for item in chosen)
        for text in ("3 月30", "4月19", "4月20", "4 月21"):
            self.assertIn(text, dates)
        for text in ("第二段", "畢業試"):
            self.assertNotIn(text, dates)
        chosen, missing = select_calendar_dates(index, records, [1, 2, 3, 4], year=None, today=today)
        self.assertFalse(missing)
        self.assertEqual(len({item["chunk_id"] for item in chosen}), len(chosen))
        last = annotate_schedule_dates(chosen[-1]["evidence_excerpt"], today, "2026學年")
        self.assertIn("6月28日至7月1日〔2027年", last)
        self.assertNotIn("寒假", last)

    def test_planner_normalizes_structured_term_and_subject_intent(self):
        plan = normalize_plan({"action": "answer", "calendar_terms": [3, 3, True, 5, "2"],
                               "assessment": {"subject": "math", "term": 1, "kind": "unified", "date": "2026-10-07"}})
        self.assertEqual(plan["calendar_terms"], [3])
        self.assertEqual(plan["calendar_scope"], "all")
        intent, = plan["assessments"]
        self.assertEqual(intent["study_period"], "first")
        self.assertEqual(intent["dates"], [(10, 7)])
        self.assertEqual(intent["years"], [2026])
        self.assertEqual(normalize_plan({"calendar_scope": "self"})["calendar_scope"], "self")
        self.assertEqual(normalize_plan({"calendar_scope": ["all"]})["calendar_scope"], "all")

    def test_comparing_same_subject_and_term_preserves_both_assessment_kinds(self):
        regular = {"subject": "math", "term": 1, "kind": "regular", "grade": "高三"}
        unified = {"subject": "math", "term": 1, "kind": "unified", "grade": "高三"}
        plan = normalize_plan({"assessments": [regular, unified, regular, None]})
        self.assertEqual([intent["exam_type"] for intent in plan["assessments"]], ["regular", "unified"])
