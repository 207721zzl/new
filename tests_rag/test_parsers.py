import json

import pytest

from app.ingestion import parse_source


def test_parse_legacy_and_general_json(tmp_path):
    source = tmp_path / "knowledge.json"
    source.write_text(
        json.dumps(
            [
                {
                    "document_id": "policy",
                    "title": "数据政策",
                    "content": "数据默认保留三十天。",
                    "source": "policy.json",
                    "updated_at": "2026-08-12",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    documents = parse_source(source)
    assert documents[0].title == "数据政策"
    assert documents[0].content_sha256


def test_non_json_document_identity_is_stable_across_content_updates(tmp_path):
    source = tmp_path / "policy.txt"
    source.write_text("第一版内容。", encoding="utf-8")
    first = parse_source(source)[0]
    source.write_text("第二版内容。", encoding="utf-8")
    second = parse_source(source)[0]

    assert first.document_id == second.document_id
    assert first.content_sha256 != second.content_sha256


@pytest.mark.parametrize(
    ("filename", "content", "doc_type", "section"),
    [
        ("readme.txt", "第一段。\n\n第二段。", "text", None),
        ("readme.md", "# 产品手册\n\n## 安装\n\n执行安装命令。", "markdown", "安装"),
        (
            "readme.html",
            "<html><head><title>网页手册</title></head><body><h1>介绍</h1><p>正文。</p></body></html>",
            "html",
            "介绍",
        ),
    ],
)
def test_parse_text_formats(tmp_path, filename, content, doc_type, section):
    source = tmp_path / filename
    source.write_text(content, encoding="utf-8")
    documents = parse_source(source)
    assert documents[0].doc_type == doc_type
    assert documents[0].content
    if section:
        assert section in documents[0].blocks[0].section_path


def test_parse_docx_preserves_heading_and_table(tmp_path):
    docx = pytest.importorskip("docx")
    source = tmp_path / "manual.docx"
    document = docx.Document()
    document.add_heading("运维手册", level=1)
    document.add_paragraph("这是部署说明。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "参数"
    table.cell(0, 1).text = "值"
    table.cell(1, 0).text = "超时"
    table.cell(1, 1).text = "30 秒"
    document.save(source)

    parsed = parse_source(source)[0]
    assert parsed.title == "运维手册"
    assert {block.block_type for block in parsed.blocks} == {"paragraph", "table"}
    assert all(block.section_path == ["运维手册"] for block in parsed.blocks)


def test_parse_xlsx_creates_one_document_per_sheet(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    source = tmp_path / "catalog.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "产品"
    sheet.append(["名称", "说明"])
    sheet.append(["EvidenceRAG", "文档问答"])
    second = workbook.create_sheet("联系人")
    second.append(["角色", "姓名"])
    second.append(["负责人", "张三"])
    workbook.save(source)

    documents = parse_source(source, excel_rows_per_block=10)
    assert [document.metadata["sheet_name"] for document in documents] == ["产品", "联系人"]
    assert all(document.blocks[0].block_type == "table" for document in documents)


def test_parse_text_pdf_preserves_page_number(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    source = tmp_path / "manual.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "EvidenceRAG PDF manual")
    document.save(source)
    document.close()

    parsed = parse_source(source)[0]
    assert parsed.doc_type == "pdf"
    assert parsed.blocks[0].page_start == 1
    assert "EvidenceRAG" in parsed.content
