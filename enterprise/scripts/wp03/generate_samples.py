"""Deterministic synthetic sample PDF generator for WP-03 parsing evaluation.

Generated PDFs are written under artifacts/wp03/samples/ and are never intended
to be committed. The committed manifest contains only non-sensitive metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

SCHEMA_VERSION = 1
GROUND_TRUTH_PROVENANCE = {
    "source": "synthetic_generator",
    "human_reviewed": False,
    "note": (
        "Phase 1 engineering baseline only. Ground truth values are generated "
        "from the same deterministic sample definitions as the PDF content and "
        "are not independent human annotations."
    ),
}
DEFAULT_GT = {
    "equipment_id": "EQ-2026-0101",
    "fixed_asset_no": "FA-2026-0001",
    "model": "TYR-5000-A",
    "document_type": "manual",
    "fault_code": "E-104",
    "version": "V1.0",
    "effective_date": "2026-01-10",
}

_BODY_POOL = [
    "This document describes installation, startup, and routine inspection "
    "for the equipment identified in the metadata block.",
    "Before operation, confirm that the power supply, grounding, and safety "
    "guards are in place and that no unauthorized modifications exist.",
    "Perform the daily checklist in the following order: visual inspection, "
    "pressure check, lubrication, control test, and fault code review.",
    "When fault code E-104 is displayed, stop the equipment, verify hydraulic "
    "oil level, and inspect the pressure line before restarting.",
    "Record every maintenance action with equipment id, model, version, and "
    "effective date so the service history remains auditable.",
    "The maintenance schedule is monthly for filters, quarterly for seals, "
    "and annually for calibration and certification.",
    "Use only approved spare parts and tools; do not bypass interlocks or "
    "modify safety circuits without a documented change request.",
    "After any repair, run a no-load test, then a full-load test, and archive "
    "the completed checklist in the enterprise document system.",
]

_ZH_POOL = [
    "本说明书适用于型号 TYR-5000-A 的日常操作与维护。",
    "操作前必须确认电源、接地和安全防护装置完好。",
    "出现故障码 E-104 时，先停止设备并检查液压油位。",
    "每次维护应记录设备编号、型号、版本和生效日期。",
    "严禁绕过联锁装置或擅自修改安全回路。",
]

_SCAN_LINES = [
    "Equipment Maintenance Manual",
    "Equipment ID: {equipment_id}",
    "Fixed Asset No: {fixed_asset_no}",
    "Model: {model}",
    "Document Type: {document_type}",
    "Fault Code: {fault_code}",
    "Version: {version}",
    "Effective Date: {effective_date}",
    "Daily inspection: visual check, pressure check, control test.",
    "Fault E-104: stop equipment, check oil level, inspect pressure line.",
]


def _register_fonts() -> str:
    for name, path, subfont in (
        ("MSYH", r"C:\Windows\Fonts\msyh.ttc", 0),
        ("ARIAL", r"C:\Windows\Fonts\arial.ttf", None),
    ):
        try:
            if Path(path).exists():
                if subfont is not None:
                    pdfmetrics.registerFont(TTFont(name, path, subfontIndex=subfont))
                else:
                    pdfmetrics.registerFont(TTFont(name, path))
                return name
        except Exception:
            continue
    return "Helvetica"


def _paragraphs(fields: dict[str, str], pages: int, mixed_lang: bool) -> list[str]:
    lines = [
        "Equipment ID: " + fields["equipment_id"],
        "Fixed Asset No: " + fields["fixed_asset_no"],
        "Model: " + fields["model"],
        "Document Type: " + fields["document_type"],
        "Fault Code: " + fields["fault_code"],
        "Version: " + fields["version"],
        "Effective Date: " + fields["effective_date"],
    ]
    rng = random.Random(int(fields["equipment_id"][-4:]) * 100 + pages)
    body = list(_BODY_POOL)
    if mixed_lang:
        body.extend(_ZH_POOL)
        rng.shuffle(body)
    for i in range(pages):
        lines.append(body[(i * 3) % len(body)])
        lines.append(body[(i * 3 + 1) % len(body)])
        lines.append(body[(i * 3 + 2) % len(body)])
    return lines


def _make_diagram_image(kind: str, width: int = 1600, height: int = 1000) -> Image.Image:
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 28)
    small = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 20)
    nodes = []
    if kind == "flow":
        labels = ["START", "CHECK", "DECISION", "ACTION", "END"]
        for i, label in enumerate(labels):
            x = 60 + i * (width - 120) // len(labels)
            y = height // 2
            nodes.append((x, y, label))
            color = (0, 0, 255) if label == "DECISION" else (0, 128, 0)
            draw.rectangle([x, y, x + 170, y + 60], outline=color, width=4)
            draw.text((x + 15, y + 14), label, font=font, fill="black")
        for i in range(len(nodes) - 1):
            x1 = nodes[i][0] + 170
            y1 = nodes[i][1] + 30
            x2 = nodes[i + 1][0]
            y2 = nodes[i + 1][1] + 30
            draw.line([x1, y1, x2, y2], fill="black", width=4)
            draw.polygon(
                [(x2 - 10, y2 - 10), (x2, y2), (x2 - 10, y2 + 10)],
                fill="black",
            )
    elif kind == "structure":
        boxes = [
            ("EQUIPMENT", 100, 100, 360, 160),
            ("CONTROL", 150, 260, 330, 320),
            ("POWER", 420, 260, 600, 320),
            ("SENSOR", 150, 430, 330, 490),
            ("ACTUATOR", 420, 430, 600, 490),
        ]
        for label, x1, y1, x2, y2 in boxes:
            draw.rectangle([x1, y1, x2, y2], outline=(128, 0, 128), width=4)
            draw.text((x1 + 25, y1 + 25), label, font=font, fill="black")
        for a, b in [(0, 1), (0, 2), (1, 3), (2, 4)]:
            _, x1, y1, x2, y2 = boxes[a]
            _, x3, y3, x4, y4 = boxes[b]
            draw.line([(x1 + x2) // 2, y2, (x3 + x4) // 2, y3], fill="black", width=3)
    elif kind == "operation":
        for i in range(5):
            x = 80 + i * 320
            y = 120
            draw.rectangle([x, y, x + 220, y + 150], outline=(255, 165, 0), width=4)
            draw.text((x + 20, y + 45), f"STEP {i + 1}", font=font, fill="black")
            if i:
                draw.line([(x - 20, y + 75), (x - 2, y + 75)], fill="black", width=3)
        draw.text(
            (60, 420),
            "Operation diagram: power off, isolate, inspect, repair, test.",
            font=small,
            fill="black",
        )
    else:
        draw.rectangle([120, 120, 500, 240], outline=(0, 0, 128), width=4)
        draw.ellipse([620, 130, 900, 390], outline=(0, 128, 128), width=4)
        draw.line([(500, 180), (620, 260)], fill="black", width=3)
        draw.text((150, 150), "MAIN DEVICE", font=font, fill="black")
        draw.text((650, 230), "NETWORK", font=font, fill="black")
    return img


def _make_table_rows(count: int, fields: dict[str, str]) -> list[list[str]]:
    rows = [
        ["Item", "Equipment ID", "Model", "Fault Code", "Version", "Date"],
    ]
    for i in range(count):
        rows.append(
            [
                str(i + 1),
                fields["equipment_id"],
                fields["model"],
                fields["fault_code"],
                fields["version"],
                fields["effective_date"],
            ]
        )
    return rows


def _build_digital(
    path: Path,
    pages: int,
    fields: dict[str, str],
    mixed_lang: bool = False,
    landscape_page: bool = False,
    tables: list[int] | None = None,
    images: list[int] | None = None,
    diagram_kind: str | None = None,
) -> None:
    font_name = _register_fonts()
    page_size = landscape(A4) if landscape_page else A4
    doc = SimpleDocTemplate(str(path), pagesize=page_size)
    style = ParagraphStyle(
        "body",
        fontName=font_name,
        fontSize=11,
        leading=16,
        spaceAfter=8,
    )
    title_style = ParagraphStyle(
        "title",
        fontName=font_name,
        fontSize=18,
        leading=24,
        spaceAfter=12,
    )
    story = [Paragraph(f"Sample {path.stem}", title_style)]
    content_lines = _paragraphs(fields, pages, mixed_lang)
    table_pages = set(tables or [])
    image_pages = set(images or [])
    temp_images: list[Path] = []
    for page_no in range(1, pages + 1):
        if page_no > 1:
            story.append(PageBreak())
        if page_no == 1:
            page_lines = content_lines[:7]
        else:
            start = 7 + (page_no - 2) * 3
            page_lines = content_lines[start : start + 3]
        if page_no in table_pages:
            # Keep the metadata block on table pages so every ground truth field
            # actually exists in the source PDF.
            page_lines = content_lines[:7]
        for line in page_lines:
            story.append(Paragraph(line, style))
        if page_no in table_pages:
            table = Table(_make_table_rows(2, fields))
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.6, colors.grey),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            story.append(table)
        if page_no in image_pages:
            image = _make_diagram_image(diagram_kind or "flow")
            with tempfile.NamedTemporaryFile(
                suffix=".png", delete=False
            ) as tmp:
                image.save(tmp.name, "PNG")
                tmp_path = tmp.name
            temp_images.append(Path(tmp_path))
            story.append(
                RLImage(
                    tmp_path,
                    width=150 * mm,
                    height=90 * mm,
                )
            )
    doc.build(story)
    for tmp_path in temp_images:
        tmp_path.unlink(missing_ok=True)


def _build_scan(
    path: Path,
    pages: int,
    fields: dict[str, str],
    mode: str,
    seed: int,
    landscape_page: bool = False,
) -> None:
    rng = random.Random(seed)
    width, height = (1754, 1240) if landscape_page else (1240, 1754)
    images: list[Image.Image] = []
    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 28)
    lines = [line.format(**fields) for line in _SCAN_LINES]
    for page_no in range(1, pages + 1):
        img = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(img)
        y = 90
        for line in lines:
            draw.text((110, y), f"Page {page_no}: {line}", font=font, fill="black")
            y += 56
        for _ in range(6):
            x1 = rng.randint(60, width - 200)
            x2 = rng.randint(x1 + 50, width - 60)
            y1 = rng.randint(60, height - 120)
            y2 = rng.randint(y1 + 50, height - 60)
            draw.rectangle([x1, y1, x2, y2], outline=(120, 120, 120), width=2)
        if mode in ("blur", "poor"):
            img = img.filter(ImageFilter.GaussianBlur(2))
        if mode in ("tilt", "poor"):
            img = img.rotate(3, expand=True, fillcolor="white")
            img = img.resize((width, height))
        if mode in ("noise", "poor"):
            noise = Image.effect_noise((width, height), 24).convert("RGB")
            img = Image.blend(img, noise, 0.08)
        images.append(img)
    images[0].save(
        str(path),
        "PDF",
        save_all=True,
        append_images=images[1:],
        resolution=150,
    )


def _sample_fields(index: int) -> dict[str, str]:
    fields = dict(DEFAULT_GT)
    fields["equipment_id"] = f"EQ-2026-{index:04d}"
    fields["fixed_asset_no"] = f"FA-2026-{index:04d}"
    fields["model"] = f"TYR-{5000 + index}-A"
    fields["document_type"] = "manual" if index % 2 else "inspection"
    fields["fault_code"] = f"E-{100 + index}"
    fields["version"] = f"V1.{index % 5}"
    fields["effective_date"] = f"2026-{1 + (index % 12):02d}-15"
    return fields


SAMPLE_DEFS: list[dict[str, Any]] = []
PAGES_BY_INDEX = {
    1: 3, 2: 4, 3: 5, 4: 2, 5: 5, 6: 2, 7: 4, 8: 1, 9: 2, 10: 3,
    11: 2, 12: 2, 13: 2, 14: 2, 15: 2, 16: 4, 17: 3, 18: 3, 19: 2,
    20: 4, 21: 2, 22: 3, 23: 2, 24: 1, 25: 2, 26: 1, 27: 3, 28: 2,
}
for _index in range(1, 29):
    _fields = _sample_fields(_index)
    _kind: str
    _pages = PAGES_BY_INDEX[_index]
    _landscape = False
    _tables: list[int] | None = None
    _images: list[int] | None = None
    _diagram: str | None = None
    _mixed = False
    _low = False
    _expected_status: str | None = "passed"
    _citation_questions: list[dict[str, Any]] | None = None
    if _index <= 6:
        _kind = "zh_en_mixed" if _index >= 5 else "digital_text"
        _mixed = _index >= 5
        if _index == 4:
            _landscape = True
    elif _index <= 10:
        _kind = "clear_scan"
        _expected_status = None
    elif _index <= 14:
        _kind = "degraded_scan"
        _low = True
        _expected_status = "review_required" if _index == 14 else None
    elif _index <= 18:
        _kind = "mixed_manual"
        _images = [1 + ((_index - 15) % 2)]
        if _index in (16, 17):
            _tables = [2]
    elif _index <= 22:
        _kind = "table_dense"
        _tables = list(range(1, _pages + 1))
        if _index == 21:
            _landscape = True
    elif _index == 27:
        _kind = "digital_text"
        _expected_status = "passed"
    elif _index == 28:
        _kind = "clear_scan"
        _expected_status = None
    else:
        _kind = "diagram"
        _images = list(range(1, _pages + 1))
        _diagram = ("flow", "structure", "operation", "network")[(_index - 23) % 4]
    if _index in (1, 2, 19, 23):
        _citation_questions = [
            {
                "question": "What equipment id is listed in this document?",
                "expected_page": 1,
            }
        ]
    SAMPLE_DEFS.append(
        {
            "sample_id": f"wp03-{_kind}-{_index:03d}",
            "category": _kind,
            "file_name": f"wp03-{_kind}-{_index:03d}.pdf",
            "pages": _pages,
            "landscape": _landscape,
            "parser_profile": "default",
            "ground_truth_fields": _fields,
            "expected_tables": _tables,
            "expected_images": _images,
            "low_quality": _low,
            "expected_quality_status": _expected_status,
            "citation_questions": _citation_questions,
            "notes": "synthetic, non-sensitive",
        }
    )


def build_sample(spec: dict[str, Any], output_dir: Path) -> Path:
    sample_id = spec["sample_id"]
    if sample_id.startswith("wp03-degraded_scan"):
        mode = ("blur", "tilt", "noise", "poor")[int(sample_id[-3:]) - 11]
        _build_scan(
            output_dir / spec["file_name"],
            int(spec["pages"]),
            spec["ground_truth_fields"],
            mode,
            seed=20260806 + int(sample_id[-3:]),
            landscape_page=bool(spec.get("landscape")),
        )
    elif sample_id.startswith("wp03-clear_scan"):
        _build_scan(
            output_dir / spec["file_name"],
            int(spec["pages"]),
            spec["ground_truth_fields"],
            "clear",
            seed=20260806 + int(sample_id[-3:]),
            landscape_page=bool(spec.get("landscape")),
        )
    else:
        _build_digital(
            output_dir / spec["file_name"],
            int(spec["pages"]),
            spec["ground_truth_fields"],
            mixed_lang="zh_en_mixed" in sample_id,
            landscape_page=bool(spec.get("landscape")),
            tables=spec.get("expected_tables"),
            images=spec.get("expected_images"),
            diagram_kind=(
                ("flow", "structure", "operation", "network")[
                    (int(sample_id[-3:]) - 23) % 4
                ]
                if sample_id.startswith("wp03-diagram")
                else None
            ),
        )
    return output_dir / spec["file_name"]


def write_manifest(path: Path) -> dict[str, Any]:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "name": "WP-03 parsing quality samples",
        "seed": 20260806,
        "samples_dir": "artifacts/wp03/samples",
        "generator": "enterprise/scripts/wp03/generate_samples.py",
        "ground_truth_provenance": GROUND_TRUTH_PROVENANCE,
        "samples": SAMPLE_DEFS,
    }
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="artifacts/wp03/samples")
    parser.add_argument("--manifest", default="enterprise/scripts/wp03/sample_manifest.json")
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for spec in SAMPLE_DEFS:
        if args.only and spec["sample_id"] not in args.only.split(","):
            continue
        build_sample(spec, output_dir)
    if args.write_manifest:
        write_manifest(Path(args.manifest))
    print(f"generated {len(SAMPLE_DEFS)} sample definitions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
