"""extraction / pixels."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from pypdf import PdfReader

from articles_to_anki.extraction.types import ExtractionConfig, Token


def yellow_mask(image: Image.Image) -> np.ndarray:
    """Return a permissive mask for translucent warm-yellow marker ink."""
    pixels = np.asarray(image.convert("RGB"))
    red = pixels[:, :, 0].astype(np.int16)
    green = pixels[:, :, 1].astype(np.int16)
    blue = pixels[:, :, 2].astype(np.int16)
    return (
        (red > 180)
        & (green > 160)
        & (blue < 210)
        & ((red - blue) > 30)
        & ((green - blue) > 20)
    )


def _score_tokens(
    tokens: list[Token],
    mask: np.ndarray,
    image: Image.Image,
    page,
    config: ExtractionConfig,
    annotation_regions: list[tuple[float, float, float, float]] | None = None,
) -> None:
    scale_x = image.width / float(page.width)
    scale_y = image.height / float(page.height)

    for token in tokens:
        x0 = max(0, int(token.x0 * scale_x))
        x1 = min(image.width, int(token.x1 * scale_x) + 1)
        y0 = max(0, int(token.top * scale_y))
        y1 = min(image.height, int(token.bottom * scale_y) + 1)
        if x1 <= x0 or y1 <= y0:
            continue

        crop = mask[y0:y1, x0:x1]
        pixel_coverage = float(crop.mean()) if crop.size else 0.0
        annotation_coverage = max(
            (
                _rectangle_overlap_ratio(
                    (token.x0, token.top, token.x1, token.bottom),
                    region,
                )
                for region in (annotation_regions or [])
            ),
            default=0.0,
        )
        token.coverage = max(pixel_coverage, annotation_coverage)
        height_px = y1 - y0
        above = mask[max(0, y0 - height_px) : y0, x0:x1]
        below = mask[y1 : min(image.height, y1 + height_px), x0:x1]
        above_ratio = float(above.mean()) if above.size else 0.0
        below_ratio = float(below.mean()) if below.size else 0.0
        token.vertical_spill = max(above_ratio, below_ratio)
        brush_selected = (
            token.height >= config.min_brush_token_height_pt
            and (
                (
                    pixel_coverage >= config.min_coverage
                    and token.vertical_spill < config.max_vertical_spill
                )
                or (
                    pixel_coverage >= config.min_partial_coverage
                    and token.vertical_spill <= config.max_partial_vertical_spill
                )
            )
        )
        token.selected = (
            (brush_selected or annotation_coverage >= config.min_annotation_overlap)
            and bool(re.search(r"[A-Za-z]", token.text))
        )


def _extract_highlight_regions(
    pdf_path: Path,
) -> list[list[tuple[float, float, float, float]]]:
    """Return native PDF highlight quads in pdfplumber's top-origin coordinates."""
    reader = PdfReader(pdf_path)
    pages: list[list[tuple[float, float, float, float]]] = []
    for page in reader.pages:
        crop_box = page.cropbox
        crop_left = float(crop_box.left)
        crop_bottom = float(crop_box.bottom)
        page_height = float(crop_box.height)
        regions: list[tuple[float, float, float, float]] = []
        for annotation_reference in page.get("/Annots") or []:
            annotation = annotation_reference.get_object()
            if str(annotation.get("/Subtype")) != "/Highlight":
                continue
            quad_points = annotation.get("/QuadPoints")
            if quad_points and len(quad_points) % 8 == 0:
                for offset in range(0, len(quad_points), 8):
                    regions.append(
                        _pdf_quad_to_region(
                            [float(value) for value in quad_points[offset : offset + 8]],
                            crop_left=crop_left,
                            crop_bottom=crop_bottom,
                            page_height=page_height,
                        )
                    )
                continue
            rectangle = annotation.get("/Rect")
            if rectangle and len(rectangle) == 4:
                x0, y0, x1, y1 = [float(value) for value in rectangle]
                regions.append(
                    (
                        min(x0, x1) - crop_left,
                        page_height - (max(y0, y1) - crop_bottom),
                        max(x0, x1) - crop_left,
                        page_height - (min(y0, y1) - crop_bottom),
                    )
                )
        pages.append(regions)
    return pages


def _pdf_quad_to_region(
    points: list[float],
    *,
    crop_left: float,
    crop_bottom: float,
    page_height: float,
) -> tuple[float, float, float, float]:
    xs = points[0::2]
    ys = points[1::2]
    return (
        min(xs) - crop_left,
        page_height - (max(ys) - crop_bottom),
        max(xs) - crop_left,
        page_height - (min(ys) - crop_bottom),
    )


def _rectangle_overlap_ratio(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    return intersection / area if area else 0.0


def _write_debug_image(
    image: Image.Image,
    tokens: list[Token],
    page,
    destination: Path,
    config: ExtractionConfig,
) -> None:
    debug = image.copy()
    draw = ImageDraw.Draw(debug)
    scale_x = image.width / float(page.width)
    scale_y = image.height / float(page.height)
    for token in tokens:
        if token.coverage < config.min_coverage:
            continue
        box = (
            int(token.x0 * scale_x),
            int(token.top * scale_y),
            int(token.x1 * scale_x),
            int(token.bottom * scale_y),
        )
        color = "#00a000" if token.selected else "#d03030"
        width = max(2, round(config.render_dpi / 90))
        draw.rectangle(box, outline=color, width=width)
        draw.text(
            (box[0], max(0, box[1] - 12)),
            f"{token.coverage:.2f}",
            fill=color,
            stroke_width=1,
            stroke_fill="white",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    debug.save(destination)
