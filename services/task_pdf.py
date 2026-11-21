"""Local Unicode PDF exports with measured wrapping and atomic cache writes."""
from __future__ import annotations

import os
from pathlib import Path

import fitz


_FONT_CANDIDATES = (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simsun.ttc"),
                    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"))


def export_quiz_pdf(quiz, destination, *, answers=False):
    destination = Path(destination)
    pdf = fitz.open()
    font_path = next((p for p in _FONT_CANDIDATES if p.is_file()), None)
    # Without a system CJK font (a bare Linux server) use PyMuPDF's bundled CJK
    # font and embed it. The "china-t" fallback is only a reference to a viewer
    # font, which shows boxes or garbage wherever that font is not installed.
    font = fitz.Font(fontfile=str(font_path)) if font_path else fitz.Font("cjk")
    name = "quizfont"
    page, y = None, 0
    def new_page():
        nonlocal page, y
        if len(pdf) >= 150:
            raise ValueError("試卷超過頁數限制")
        page = pdf.new_page(width=595, height=842)
        if font_path:
            page.insert_font(fontname=name, fontfile=str(font_path))
        else:
            page.insert_font(fontname=name, fontbuffer=font.buffer)
        y = 48
    def line(text, size):
        nonlocal y
        if y + size * 1.8 > 790:
            new_page()
        page.insert_text((48, y + size), text, fontname=name, fontsize=size, color=(.12, .16, .22))
        y += size * 1.6
    def write(text, size=11):
        nonlocal y
        for paragraph in str(text).split("\n"):
            pending = ""
            for char in paragraph:
                if pending and font.text_length(pending + char, fontsize=size) > 499:
                    line(pending, size)
                    pending = ""
                pending += char
            line(pending, size)
        y += 6
    try:
        new_page()
        write(quiz.get("filename", "文件練習題") + (" · 答案解析" if answers else " · 練習題"), 17)
        write(f'{len(quiz["questions"])} 題　姓名：____________　班級：____________', 10)
        labels = {"original": "原題", "variant": "微改題", "source_based": "依據資料新編"}
        for index, q in enumerate(quiz["questions"]):
            if y > 680:
                new_page()
            write(f'{index + 1}. 【{labels.get(q.get("origin"), "練習題")}】{q["stem"]}')
            for option, value in zip("ABCD", q.get("options", [])):
                write(f"{option}. {value}")
            if answers:
                write("參考答案：" + q["answer"])
                write("解析：" + q["explanation"])
                write(f'來源：{q.get("filename", quiz.get("filename", ""))} · 第{q.get("page", 1)}頁', 9)
                write("原文依據：" + q.get("evidence", ""), 9)
            elif q["type"] != "choice":
                for _ in range(3 if q["type"] == "short" else 1):
                    write("________________________________________________________________")
            y += 12
        for index, page in enumerate(pdf):
            page.insert_text((275, 817), f"{index + 1} / {len(pdf)}", fontsize=9, color=(.45, .45, .45))
        temporary = destination.with_suffix(".tmp.pdf")
        pdf.save(temporary, deflate=True)
        os.replace(temporary, destination)
    finally:
        pdf.close()
