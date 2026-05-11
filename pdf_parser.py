import json
import logging
import os
import base64
import io
from typing import Any, Dict, List, Tuple
from collections import Counter
from dataclasses import dataclass, field
import math
from xml.sax.saxutils import escape
from colorama import Fore, Style


import fitz  # PyMuPDF
import pdfplumber
from dotenv import load_dotenv
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Table, TableStyle, Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors as rl_colors


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
DEBUG_EXTRACTION = os.getenv("PDF_TEMPLATER_DEBUG", "").lower() in {"1", "true", "yes", "on"}


def debug_print(*args):
    pass

def table_debug(*args):
    #print(*args)
    pass

# ==========================================
# MODELS
# ==========================================

@dataclass
class PageElement:
    type: str
    y_pos: float
    y1_pos: float
    x_pos: float
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "y_pos": self.y_pos,
            "y1_pos": self.y1_pos,
            "x_pos": self.x_pos,
            **self.data 
        }

# ==========================================
# UTILS & HELPERS
# ==========================================

class StyleManager:
    @staticmethod
    def normalize_font_size(size: float) -> float:
        return round(size * 0.85, 1)

    @staticmethod
    def is_bold_font(font_name: str, flags: int) -> bool:
        return "Bold" in font_name or flags & 2**4

# ==========================================
# EXTRACTION MODULES
# ==========================================

class ExtractionContext:
    def __init__(self, fitz_page: fitz.Page, plumb_page: pdfplumber.page.Page):
        self.fitz_page = fitz_page
        self.plumb_page = plumb_page
        self.exclusion_bboxes: List[Tuple[float, float, float, float]] = []
        self.elements: List[PageElement] = []

    def add_exclusion(self, bbox: Tuple[float, float, float, float]):
        self.exclusion_bboxes.append(bbox)

    def is_excluded(self, bbox: Tuple[float, float, float, float], threshold: float = 0.1) -> bool:
        rect = fitz.Rect(bbox)
        for ex in self.exclusion_bboxes:
            if fitz.Rect(ex).intersects(rect):
                return True
        return False

class TableExtractor:
    
    # Checks if text's center is within the cell, and extracts it

    @staticmethod
    def _extract_text_from_bbox(fitz_page, bbox):
        bx0, by0, bx1, by1 = bbox
        text_dict = fitz_page.get_text("dict", clip=fitz.Rect(bx0, by0, bx1, by1))

        words_in_cell = []

        for block in text_dict.get("blocks", []):
            if block.get("type", 0) != 0: continue

            for line in block["lines"]:
                if line["bbox"][1] < by0 or line["bbox"][1] > by1:
                    continue

                for span in line["spans"]:
                    color = span.get("color", 0)
                    r = (color >> 16) & 0xFF
                    g = (color >> 8) & 0xFF
                    b = color & 0xFF
                    luminance = 0.299 * r + 0.587 * g + 0.114 * b
                    if luminance > 100:
                        continue  # too light — ghost/invisible text
                    sx0, sy0, sx1, sy1 = span["bbox"]
                    cx = (sx0 + sx1) / 2.0
                    if not (bx0 <= cx <= bx1):
                        continue
                    for word in span["text"].split():
                        if word.strip():
                            words_in_cell.append((span["bbox"][1], span["bbox"][0], word))
            
        if not words_in_cell: 
            return ""
            
        words_in_cell.sort(key=lambda item: (round(item[0] / 3) * 3, item[1]))
        return " ".join([item[2] for item in words_in_cell])

    # original function, hid it cause im working on rewriting it
    # @staticmethod
    # def _extract_text_from_bbox(fitz_page, bbox):
    #     bx0, by0, bx1, by1 = bbox
    #     words = fitz_page.get_text("words", clip=fitz.Rect(bx0, by0, bx1, by1))

    #     words_in_cell = []
    #     for w in words:
    #         x0, y0, x1, y1, text = w[:5]

    #         cx = (x0 + x1) / 2.0
            
    #         if bx0 <= cx <= bx1 and by0 <= y0 <= by1:
    #             words_in_cell.append((y0, x0, text))
            
    #     if not words_in_cell: 
    #         return ""
            
    #     words_in_cell.sort(key=lambda item: (round(item[0] / 3) * 3, item[1]))
    #     return " ".join([item[2] for item in words_in_cell])

    @staticmethod
    def _assign_text_via_fitz(fitz_page, col_xs, row_ys):
        words = fitz_page.get_text("words")
        n_cols = len(col_xs) - 1
        n_rows = len(row_ys) - 1
        
        grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
        cell_words = [[[] for _ in range(n_cols)] for _ in range(n_rows)]
        
        for w in words:
            x0, y0, x1, y1, text = w[:5]
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            
            c_idx = -1
            for c in range(n_cols):
                if col_xs[c] <= cx <= col_xs[c+1]:
                    c_idx = c
                    break
                    
            r_idx = -1
            for r in range(n_rows):
                if row_ys[r] <= cy <= row_ys[r+1]:
                    r_idx = r
                    break
                    
            if c_idx != -1 and r_idx != -1:
                cell_words[r_idx][c_idx].append((y0, x0, text))
                
        for r in range(n_rows):
            for c in range(n_cols):
                words_in_cell = cell_words[r][c]
                if not words_in_cell:
                    grid[r][c] = ""
                else:
                    # Sort roughly by Y (grouping lines within ~3 points) then by X
                    words_in_cell.sort(key=lambda item: (round(item[0] / 3) * 3, item[1]))
                    grid[r][c] = " ".join([item[2] for item in words_in_cell])
                    
        return grid

    @staticmethod
    def extract(ctx: ExtractionContext):
        tables = ctx.plumb_page.find_tables(table_settings={
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "snap_tolerance": 10,
            "join_tolerance": 6,
        })
        
        table_debug(Fore.RED + "tables/page: ", len(tables), Style.RESET_ALL)
        for t in tables:
            # --- Collect all unique x-coordinates from every cell ---
            x_coords = set()
            for row_cells in t.rows:
                for cell in row_cells.cells:
                    if cell is None:
                        continue
                    x0, _, x1, _ = cell
                    x_coords.add(round(x0, 1))
                    x_coords.add(round(x1, 1))
            col_boundaries = sorted(x_coords)
            n_cols = len(col_boundaries) - 1
            
            # Column widths from the sorted boundaries
            col_widths = {"pt": [round(col_boundaries[i+1] - col_boundaries[i], 1)
                                for i in range(n_cols)]}

            # Row y-boundaries — from every row's bbox
            y_coords = set()
            for row in t.rows:
                y_coords.add(round(row.bbox[1], 1))
                y_coords.add(round(row.bbox[3], 1))
            row_boundaries = sorted(y_coords)
            n_rows = len(row_boundaries) - 1
            
            row_heights = [round(row_boundaries[i+1] - row_boundaries[i], 1) for i in range(n_rows)]

            def find_col_idx(x):
                return min(range(len(col_boundaries)), key=lambda i: abs(col_boundaries[i] - x))

            def find_row_idx(y):
                return min(range(len(row_boundaries)), key=lambda i: abs(row_boundaries[i] - y))

            unique_cells = {}
            for row_cells in t.rows:
                for cell in row_cells.cells:
                    if cell is not None:
                        rounded_key = tuple(round(v, 1) for v in cell)
                        if rounded_key not in unique_cells:
                            unique_cells[rounded_key] = cell

            grid_cells = [[None for _ in range(n_cols)] for _ in range(n_rows)]


            for rounded_key, orig_cell in unique_cells.items():
                x0, y0, x1, y1 = rounded_key

                start_col = find_col_idx(x0)
                end_col = find_col_idx(x1)
                colspan = max(1, end_col - start_col)
                start_col = min(start_col, max(0, n_cols - 1))

                start_row = find_row_idx(y0)
                end_row = find_row_idx(y1)
                rowspan = max(1, end_row - start_row)
                start_row = min(start_row, max(0, n_rows - 1))

                _, size, is_bold = TableExtractor._extract_cell_style(ctx, orig_cell)
                
                txt = TableExtractor._extract_text_from_bbox(ctx.fitz_page, orig_cell).strip()

                existing = grid_cells[start_row][start_col]
                if existing is not None:
                    if (colspan * rowspan) <= (existing["colspan"] * existing["rowspan"]):
                        continue

                grid_cells[start_row][start_col] = {
                    "text": txt,
                    "size": size,
                    "is_bold": is_bold,
                    "colspan": colspan,
                    "rowspan": rowspan
                }
                
            covered = set()
            for r in range(n_rows):
                for c in range(n_cols):
                    if grid_cells[r][c] is not None:
                        rs = grid_cells[r][c]["rowspan"]
                        cs = grid_cells[r][c]["colspan"]
                        for dr in range(rs):
                            for dc in range(cs):
                                if dr == 0 and dc == 0:
                                    continue
                                covered.add((r + dr, c + dc))
            
            for r, c in covered:
                if r < n_rows and c < n_cols:
                    grid_cells[r][c] = None

            rows = []
            for r in range(n_rows):
                row_data = []
                for c in range(n_cols):
                    if grid_cells[r][c] is not None:
                        row_data.append(grid_cells[r][c])
                rows.append(row_data)

            table_bbox_h = t.bbox[3] - t.bbox[1]
            
            ctx.elements.append(PageElement(
                type="table",
                y_pos=t.bbox[1],
                y1_pos=t.bbox[3],
                x_pos=t.bbox[0],
                data={
                    "table_width_pt": round(t.bbox[2] - t.bbox[0], 1),
                    "rows": rows,
                    "col_widths": col_widths,
                    "row_heights": row_heights,
                    "n_cols": n_cols,
                    "n_rows": len(rows),
                }
            ))
            x0, y0, x1, y1 = t.bbox
            pad = 2
            ctx.add_exclusion((x0 - pad, y0 - pad, x1 + pad, y1 + pad))

    @staticmethod
    def _extract_cell_style(ctx: ExtractionContext, cell_bbox: Tuple[float, float, float, float]):
        clip = fitz.Rect(cell_bbox)
        cell_dict = ctx.fitz_page.get_text("dict", clip=clip)

        sizes = []
        is_bold = False
        for block in cell_dict.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    sizes.append(span["size"])
                    if StyleManager.is_bold_font(span["font"], span["flags"]):
                        is_bold = True

        avg_size = sum(sizes) / len(sizes) if sizes else 11.0
        return "", StyleManager.normalize_font_size(avg_size), is_bold

class BoxExtractor:
    @staticmethod
    def extract(ctx: ExtractionContext):
        drawings = ctx.fitz_page.get_drawings()
        debug_print(f"Total drawings: {len(drawings)}")
        filtered_drawings = []
        for d in drawings:
            drawing_bbox = (d['rect'][0], d['rect'][1], d['rect'][2], d['rect'][3])
            if ctx.is_excluded(drawing_bbox):
                continue
            if not (abs(d['rect'][0] - d['rect'][2]) > 10 or abs(d['rect'][1] - d['rect'][3]) > 10):
                continue
            if d['fill'] is None:
                continue
            if not (d['fill'][0] < 0.2 and d['fill'][1] < 0.2 and d['fill'][2] < 0.2):
                continue
            filtered_drawings.append(d)
        debug_print(f"Total remaining after filter: {len(filtered_drawings)}")
        filtered_drawings.sort(key=lambda d: d['rect'][1])

        groups = []
        for d in filtered_drawings:
            overlaps = False
            r = d['rect']
            if groups:
                for group in groups:
                    for rectangle in group:
                        a = rectangle['rect']
                        if (((max(a[1], a[3]) + 5 > r[1] > min(a[1], a[3]) - 5) or
                            (max(a[1], a[3]) + 5 > r[3] > min(a[1], a[3]) - 5)) and
                            ((max(a[0], a[2]) + 5 > r[0] > min(a[0], a[2]) - 5) or
                            (max(a[0], a[2]) + 5 > r[2] > min(a[0], a[2]) - 5))):

                            overlaps = True
                            break
                    if overlaps:
                        group.append(d)
                        break
                if not overlaps:
                    groups.append([d])
                    debug_print(f"NEW GROUP started with: y0={r[1]:.1f} y1={r[3]:.1f} x0={r[0]:.1f} x1={r[2]:.1f}")
            else:
                groups.append([d])
                debug_print(f"NEW GROUP started with: y0={r[1]:.1f} y1={r[3]:.1f} x0={r[0]:.1f} x1={r[2]:.1f}")

        debug_print(f"Found {len(groups)} groups")

        for group in groups:
            minx = float('inf')
            maxx = float('-inf')
            miny = float('inf')
            maxy = float('-inf')
            for r in group:
                if r['rect'][0] < minx: minx = r['rect'][0]
                if r['rect'][3] < miny: miny = r['rect'][3]
                if r['rect'][1] > maxy: maxy = r['rect'][1]
                if r['rect'][2] > maxx: maxx = r['rect'][2]
            bbox = (minx, miny, maxx, maxy)

            page_w = ctx.fitz_page.rect.width
            page_h = ctx.fitz_page.rect.height

            if (maxx-minx)/page_w > 0.8 and  (maxy-miny)/page_h > 0.8:
                ctx.elements.append(PageElement(
                    type="page_border",
                    y_pos=miny,
                    y1_pos=maxy,
                    x_pos=minx,
                    data={
                        "border_pieces": [
                            (box['rect'][0], box['rect'][1], box['rect'][2], box['rect'][3])
                            for box in group
                        ]
                    }
                ))
                continue

            inner_lines = BoxExtractor._extract_inner_text(ctx, bbox)

            if not inner_lines: 
                continue

            ctx.elements.append(PageElement(
                type="bordered_box",
                y_pos=miny,
                y1_pos=maxy,
                x_pos=minx,
                data={
                    "lines": inner_lines,
                    "border_pieces": [
                        (box['rect'][0], box['rect'][1], box['rect'][2], box['rect'][3])
                        for box in group
                    ]
                }
            ))
            ctx.add_exclusion(bbox)

    @staticmethod
    def _extract_inner_text(ctx: ExtractionContext, bbox: Tuple[float, float, float, float]):
        clip = fitz.Rect(bbox)
        inner_dict = ctx.fitz_page.get_text("dict", clip=clip)

        lines = []
        for block in inner_dict.get("blocks", []):
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                segments = []
                for span in line["spans"]:
                    segments.append({
                        "text": span["text"],
                        "is_bold": StyleManager.is_bold_font(span["font"], span["flags"]),
                        "size": StyleManager.normalize_font_size(span["size"])
                    })
                if segments:
                    lines.append({
                        "segments": segments,
                        "y_pos": line["bbox"][1],
                        "y1_pos": line["bbox"][3],
                        "x_pos": line["bbox"][0],
                    })
        return lines

class ImageExtractor:
    @staticmethod
    def extract(ctx: ExtractionContext, fitz_doc: fitz.Document):
        for img in ctx.fitz_page.get_images():
            xref = img[0]
            img_data = fitz_doc.extract_image(xref)["image"]
            img_b64 = base64.b64encode(img_data).decode()

            for bbox in ctx.fitz_page.get_image_rects(xref):
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
                ctx.elements.append(PageElement(
                    type="logo_placeholder",
                    y_pos=bbox[1],
                    y1_pos=bbox[3],
                    x_pos=bbox[0],
                    data={"b64": img_b64, "width": w, "height": h}
                ))

# class BodyExtractor:
#     @staticmethod
#     def extract(ctx: ExtractionContext):
#         text_dict = ctx.fitz_page.get_text("dict")
#         for block in text_dict.get("blocks", []):
#             if block["type"] != 0 or ctx.is_excluded(block["bbox"]):
#                 continue
#             for line in block["lines"]:
#                 segments = []
#                 for span in line["spans"]:
#                     segments.append({
#                         "text": span["text"],
#                         "is_bold": StyleManager.is_bold_font(span["font"], span["flags"]),
#                         "size": StyleManager.normalize_font_size(span["size"])
#                     })
#                 if segments and "".join(s["text"] for s in segments).strip():
#                     ctx.elements.append(PageElement(
#                         type="body",
#                         y_pos=line["bbox"][1],
#                         y1_pos=line["bbox"][3],
#                         x_pos=line["bbox"][0],
#                         data={"segments": segments}
#                     ))


class BodyExtractor:
    @staticmethod
    def extract(ctx: ExtractionContext):
        text_dict = ctx.fitz_page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block["type"] != 0:
                continue
            if ctx.is_excluded(block["bbox"]):
                debug_print("SKIPPING EXCLUDED BODY BLOCK:", block["bbox"])
                continue
            raw_full_text = "".join(
                span["text"]
                for line in block.get("lines", [])
                for span in line.get("spans", [])
            )
            first_span_text = ""
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if spans:
                    first_span_text = spans[0].get("text", "")
                    break
            debug_introduction = "Introduction" in raw_full_text or "Introduction" in first_span_text
            if debug_introduction:
                debug_print("INTRO DEBUG - raw block bbox:", block["bbox"])
                debug_print("INTRO DEBUG - raw line count:", len(block.get("lines", [])))
                for line_idx, raw_line in enumerate(block.get("lines", [])):
                    raw_line_text = "".join(span.get("text", "") for span in raw_line.get("spans", []))
                    debug_print(f"INTRO DEBUG - line {line_idx} bbox:", raw_line.get("bbox"))
                    debug_print(f"INTRO DEBUG - line {line_idx} text:", raw_line_text)
                    for span_idx, raw_span in enumerate(raw_line.get("spans", [])):
                        debug_print(
                            f"INTRO DEBUG - line {line_idx} span {span_idx}:",
                            {
                                "text": raw_span.get("text"),
                                "font": raw_span.get("font"),
                                "size": raw_span.get("size"),
                                "flags": raw_span.get("flags"),
                                "bbox": raw_span.get("bbox"),
                            }
                        )

            lines = []
            for line in block["lines"]:
                line_segments = []
                for span in line["spans"]:
                    segment = {
                        "text": span["text"],
                        "is_bold": StyleManager.is_bold_font(span["font"], span["flags"]),
                        "size": StyleManager.normalize_font_size(span["size"]),
                        "bbox": span["bbox"],
                    }
                    line_segments.append(segment)

                if line_segments:
                    lines.append({
                        "segments": line_segments,
                        "x_pos": line["bbox"][0],
                        "y_pos": line["bbox"][1],
                        "y1_pos": line["bbox"][3],
                    })
            segments = [segment for line in lines for segment in line['segments']]
            if segments and "".join(s["text"] for s in segments).strip():
                render_mode = BodyExtractor.classify_render_mode(block, lines, segments)
                final_text = "".join(s["text"] for s in segments).strip()
                if debug_introduction:
                    contains_following_text = "Introduction" in final_text and len(final_text.replace("Introduction", "").strip()) > 0
                    debug_print("INTRO DEBUG - assigned render_mode:", render_mode)
                    debug_print("INTRO DEBUG - final PageElement text:", final_text)
                    debug_print("INTRO DEBUG - contains Introduction:", "Introduction" in final_text)
                    debug_print("INTRO DEBUG - contains Introduction plus following text:", contains_following_text)
                for segment in segments:
                    segment.pop("bbox", None)
                ctx.elements.append(PageElement(
                    type ="body",
                    y_pos = block["bbox"][1],
                    y1_pos = block["bbox"][3],
                    x_pos = block["bbox"][0],
                    data = {
                        "width" : block["bbox"][2] - block["bbox"][0],
                        # "segments": block_segments, -> useless, added immense json outputs. 'lines' alone is enough
                        "lines": lines,
                        "render_mode": render_mode,
                        }
                ))

    @staticmethod
    def classify_render_mode(block, lines, segments) -> str: 
        full_text = "".join(s["text"] for s in segments).strip()
        line_texts = [
            "".join(s["text"] for s in line.get("segments", [])).strip()
            for line in lines
        ]
        non_empty_line_texts = [text for text in line_texts if text]

        if "....." in full_text:
            return "fixed_lines"

        if "." * 3 in full_text and full_text.split() and full_text.split()[-1].isdigit():
            return "fixed_lines"

        if len(lines) == 1 and len(lines[0].get("segments", [])) > 1:
            line_segments = lines[0]["segments"]
            gaps = []
            for prev, current in zip(line_segments, line_segments[1:]):
                prev_bbox = prev.get("bbox")
                current_bbox = current.get("bbox")
                if prev_bbox and current_bbox:
                    gaps.append(current_bbox[0] - prev_bbox[2])

            if any(gap > 20 for gap in gaps):
                return "fixed_lines"

        if 0 < len(non_empty_line_texts) <= 3:
            avg_line_length = sum(len(text) for text in non_empty_line_texts) / len(non_empty_line_texts)
            if avg_line_length < 60:
                return "fixed_lines"

        return "flow_paragraph"



# ==========================================
# COORDINATOR
# ==========================================

class PDFProcessor:
    def __init__(self, pdf_path: str, page_limit: int = 5):
        self.pdf_path = pdf_path
        self.page_limit = page_limit
        self.header_threshold = 75
        self.footer_threshold_offset = 50

    def run(self) -> List[Dict[str, Any]]:
        pages_data = []
        with fitz.open(self.pdf_path) as fitz_doc:
            with pdfplumber.open(self.pdf_path) as plumb_pdf:
                num_pages = min(len(plumb_pdf.pages), self.page_limit)
                for i in range(num_pages):
                    pages_data.append(self._process_page(i, fitz_doc, plumb_pdf))
        return pages_data

    def _process_page(self, page_num: int, fitz_doc, plumb_pdf) -> Dict[str, Any]:
        fitz_page = fitz_doc[page_num]
        plumb_page = plumb_pdf.pages[page_num]
        ctx = ExtractionContext(fitz_page, plumb_page)

        TableExtractor.extract(ctx)
        debug_print("after tables:", [(e.type, round(e.y_pos, 1), round(e.y1_pos, 1)) for e in ctx.elements])
        BoxExtractor.extract(ctx)
        debug_print("after boxes:", [(e.type, round(e.y_pos, 1), round(e.y1_pos, 1)) for e in ctx.elements])
        box_count = sum(1 for e in ctx.elements if e.type == "bordered_box")
        debug_print(f"Page {page_num}: {box_count} bordered boxes found")
        ImageExtractor.extract(ctx, fitz_doc)
        BodyExtractor.extract(ctx)
        for elem in ctx.elements:
            if elem.type != "body":
                continue
            joined_text = "".join(segment.get("text", "") 
                                for line in elem.data.get("lines" , [])
                                for segment in line.get("segments", [])
                                )
            if "Introduction" not in joined_text:
                continue
            debug_print(
                "INTRO DEBUG - extracted body element:",
                {
                    "type": elem.type,
                    "y_pos": elem.y_pos,
                    "y1_pos": elem.y1_pos,
                    "x_pos": elem.x_pos,
                    "width": elem.data.get("width"),
                    "render_mode": elem.data.get("render_mode"),
                    "text": joined_text,
                    "line_count": len(elem.data.get("lines", [])),
                }
            )
        debug_print("after body:", [(e.type, round(e.y_pos, 1), round(e.y1_pos, 1)) for e in ctx.elements])

        base_x = self._detect_baseline(ctx)

        ctx.elements.sort(key=lambda x: (round(x.y_pos), x.x_pos))
        footer_threshold = fitz_page.rect.height - self.footer_threshold_offset

        headers, body, footers = [], [], []
        for e in ctx.elements:
            if e.type == "logo_placeholder" or e.y_pos < self.header_threshold:
                headers.append(e.to_dict())
            elif e.y_pos > footer_threshold:
                footers.append(e.to_dict())
            else:
                body.append(e.to_dict())

        index = 0
        for item in headers:
            item['id'] = f"p{page_num}_b{index}"
            index += 1
        for item in body:
            item['id'] = f"p{page_num}_b{index}"
            index += 1
        for item in footers:
            item['id'] = f"p{page_num}_b{index}"
            index += 1

        #segment compression
        previous_bold = None
        previous_size = None
        previous_dict = None
        segment_list = []
        for item in body:
            if "lines" in item:
                for dict in item['lines']:
                    for mini_dict in dict['segments']:
                        
                        if mini_dict['is_bold'] == previous_bold and mini_dict['size'] == previous_size:
                            previous_dict['text'] = previous_dict['text'] + mini_dict['text']
                        else:
                            previous_dict = mini_dict
                            previous_bold = mini_dict['is_bold']
                            previous_size = mini_dict['size']
                            segment_list.append(mini_dict)
                    dict['segments'] = segment_list
                    segment_list = []
                    previous_dict = None
                    previous_bold = None
                    previous_size = None


        return {
            "headers": headers,
            "body": body,
            "footers": footers,
            "base_x": base_x,
            "page_height": fitz_page.rect.height,
            "page_width": fitz_page.rect.width,
        }

    def _detect_baseline(self, ctx: ExtractionContext) -> float:
        x_positions = []
        text_dict = ctx.fitz_page.get_text("dict")
        for b in text_dict.get("blocks", []):
            if b["type"] == 0 and not ctx.is_excluded(b["bbox"]):
                for l in b["lines"]:
                    x_positions.append(round(l["bbox"][0]))
        return Counter(x_positions).most_common(1)[0][0] if x_positions else 50.0

# ==========================================
# RENDERING MODULE
# ==========================================

class ReportLabRenderer:
    def __init__(self, pages_data: List[Dict[str, Any]]):
        self.pages_data = pages_data

    @staticmethod
    def segments_to_xml(segments):
        parts = []

        for segment in segments:
            text = escape(segment["text"])

            if segment["is_bold"]:
                parts.append(f"<b>{text}</b>")
            else:
                parts.append(text)

        return "".join(parts)

    @staticmethod
    def segments_to_font_xml(segments):
        parts = []

        for segment in segments:
            text = escape(segment["text"])

            if segment["is_bold"]:
                parts.append(f'<font name="Helvetica-Bold">{text}</font>')
            else:
                parts.append(text)

        return "".join(parts)

    @staticmethod
    def _looks_like_run_in_heading(elem: Dict[str, Any]) -> bool:
        if elem.get("render_mode", "flow_paragraph") != "flow_paragraph":
            return False

        segments =  [segment for line in elem.get("lines" , []) for segment in line.get("segments", [])]
        if len(segments) < 2:
            return False

        first_text = segments[0].get("text", "").strip()
        if not segments[0].get("is_bold") or not first_text or len(first_text) >= 40:
            return False

        has_following_normal_text = any(
            not segment.get("is_bold") and segment.get("text", "").strip()
            for segment in segments[1:]
        )
        if not has_following_normal_text:
            return False

        return len(elem.get("lines", [])) > 1

    def render(self, output_path: str):
        first = self.pages_data[0]
        default_pw = first.get("page_width", 595.0)
        default_ph = first.get("page_height", 792.0)

        c = rl_canvas.Canvas(output_path, pagesize=(default_pw, default_ph))

        for page in self.pages_data:
            ph = page.get("page_height", default_ph)
            pw = page.get("page_width", default_pw)
            all_elements = page["headers"] + page["body"] + page["footers"]
            for elem in all_elements:
                self._draw_element(c, elem, ph, pw)
            c.showPage()

        c.save()

    def _draw_element(self, c, elem: Dict[str, Any], ph: float, pw: float):
        t = elem["type"]
        if t == "body":
            self._draw_body(c, elem, ph)
        elif t == "table":
            self._draw_table(c, elem, ph, pw)
        elif t == "bordered_box":
            self._draw_bordered_box(c, elem, ph)
        elif t == "page_border":
            self._draw_bordered_box(c, elem, ph)
        elif t == "logo_placeholder":
            self._draw_image(c, elem, ph)

    def _draw_inline_segments(self, c, segments: List[Dict], x: float, rl_y: float):
        cur_x = x
        for seg in segments:
            font = "Helvetica-Bold" if seg["is_bold"] else "Helvetica"
            sz = seg["size"]
            c.setFont(font, sz)
            c.setFillColorRGB(0, 0, 0)
            c.drawString(cur_x, rl_y, seg["text"])
            cur_x += c.stringWidth(seg["text"], font, sz)
    
    def _draw_wrapped_paragraph(self, c, elem: Dict[str, Any], ph: float):
        width = elem.get("width")
        segments = [segment for line in elem.get("lines" , []) for segment in line.get("segments", [])]
        
        if not segments or not width:
            return

        is_run_in_heading = self._looks_like_run_in_heading(elem)
        if is_run_in_heading:
            self._draw_run_in_heading_paragraph(c, elem, ph)
            return

        xml_text = self.segments_to_font_xml(segments) if is_run_in_heading else self.segments_to_xml(segments)
        plain_text = "".join(segment.get("text", "") for segment in segments)

        font_size = segments[0].get("size", 9.5)
        style = ParagraphStyle(
            name="BodyText",
            fontName="Helvetica",
            fontSize=font_size,
            leading=font_size * 1.2,
            textColor=rl_colors.black,
            spaceBefore=0,
            spaceAfter=0,
        )

        paragraph = Paragraph(xml_text, style)

        available_height = ph - elem["y_pos"]
        _, paragraph_height = paragraph.wrapOn(c, width, available_height)
        if "Introduction" in plain_text:
            debug_print("INTRO PARAGRAPH DEBUG - run_in_heading:", is_run_in_heading)
            debug_print("INTRO PARAGRAPH DEBUG - xml_text:", xml_text)
            debug_print("INTRO PARAGRAPH DEBUG - width:", width)
            debug_print("INTRO PARAGRAPH DEBUG - font_size:", font_size)
            debug_print("INTRO PARAGRAPH DEBUG - wrapped_height:", paragraph_height)

        x = elem["x_pos"]
        y = ph - elem["y_pos"] - paragraph_height

        paragraph.drawOn(c, x, y)

    def _draw_run_in_heading_paragraph(self, c, elem: Dict[str, Any], ph: float):
        segments = [segment for line in elem.data.get("lines" , []) for segment in line.get("segments", [])]
        width = elem.get("width")
        if len(segments) < 2 or not width:
            return

        heading = segments[0]
        heading_text = heading.get("text", "")
        font_size = heading.get("size", 9.5)
        leading = font_size * 1.2
        x = elem["x_pos"]

        first_line = (elem.get("lines") or [{}])[0]
        if first_line.get("y1_pos") is not None:
            rl_y = ph - first_line["y1_pos"] + font_size * 0.2
        else:
            rl_y = ph - elem["y_pos"] - font_size
        start_rl_y = rl_y

        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", font_size)
        c.drawString(x, rl_y, heading_text)

        body_start_y = rl_y - leading
        stored_lines = elem.get("lines") or []
        if len(stored_lines) > 1 and stored_lines[1].get("y1_pos") is not None:
            body_start_y = ph - stored_lines[1]["y1_pos"] + font_size * 0.2

        rl_y = body_start_y
        cur_x = x
        normal_font = "Helvetica"
        normal_size = next(
            (segment.get("size", font_size) for segment in segments[1:] if segment.get("text", "").strip()),
            font_size
        )
        space_width = c.stringWidth(" ", normal_font, normal_size)
        remaining_width = width

        remaining_text = "".join(segment.get("text", "") for segment in segments[1:]).strip()
        words = remaining_text.split()

        c.setFont(normal_font, normal_size)
        for word in words:
            word_width = c.stringWidth(word, normal_font, normal_size)
            add_space = cur_x > x
            needed_width = word_width + (space_width if add_space else 0)

            if needed_width > remaining_width and cur_x > x:
                rl_y -= leading
                cur_x = x
                remaining_width = width
                add_space = False

            if add_space:
                cur_x += space_width

            c.drawString(cur_x, rl_y, word)
            cur_x += word_width
            remaining_width = x + width - cur_x

        plain_text = heading_text + remaining_text
        if "Introduction" in plain_text:
            debug_print("INTRO PARAGRAPH DEBUG - custom_run_in_renderer:", True)
            debug_print("INTRO PARAGRAPH DEBUG - start_x:", x)
            debug_print("INTRO PARAGRAPH DEBUG - start_y:", start_rl_y)
            debug_print("INTRO PARAGRAPH DEBUG - width:", width)
            debug_print("INTRO PARAGRAPH DEBUG - font_size:", font_size)

    def _draw_body(self, c, elem: Dict[str, Any], ph: float):
        render_mode = elem.get("render_mode", "flow_paragraph")

        if render_mode == "fixed_lines":
            self._draw_fixed_lines(c, elem, ph)
        else:
            self._draw_wrapped_paragraph(c, elem, ph)

    def _draw_fixed_lines(self, c, elem: Dict[str, Any], ph: float):
        for line in elem.get("lines", []):
            segments = line.get("segments", []) 
            if not segments:
                continue
            rl_y = ph - line["y1_pos"] + segments[0]["size"] * 0.2
            self._draw_inline_segments(c, segments, line["x_pos"], rl_y)

    def _draw_bordered_box(self, c, elem: Dict[str, Any], ph: float):
        # Each border_piece is a thin filled black rectangle (x0, y0, x1, y1) in PDF coords
        c.setFillColorRGB(0, 0, 0)
        for x0, y0, x1, y1 in elem.get("border_pieces", []):
            # rl_y is the bottom of the rect in RL coords (origin bottom-left)
            c.rect(x0, ph - y1, x1 - x0, y1 - y0, fill=1, stroke=0)

        for line in elem.get("lines", []):
            segments = line.get("segments", []) 
            if not segments:
                continue
            rl_y = ph - line["y1_pos"] + segments[0]["size"] * 0.2
            self._draw_inline_segments(c, segments, line["x_pos"], rl_y)

    def _draw_table(self, c, elem: Dict[str, Any], ph: float, pw: float):
        rows = elem.get("rows", [])
        if not rows:
            return

        col_widths_pt = (elem.get("col_widths") or {}).get("pt")
        if col_widths_pt:
            sanitized = []
            for w in col_widths_pt:
                if w is None:
                    continue
                try:
                    w = float(w)
                except (TypeError, ValueError):
                    continue
                sanitized.append(max(w, 20.0))
            col_widths_pt = sanitized

        n_cols = elem.get("n_cols") or (len(col_widths_pt) if col_widths_pt else max((len(r) for r in rows), default=1))
        n_rows = elem.get("n_rows") or len(rows)

        row_heights_pt = elem.get("row_heights")
        if row_heights_pt:
            row_heights_pt = [max(float(h), 10.0) for h in row_heights_pt]
            if len(row_heights_pt) != n_rows:
                row_heights_pt = None

        # Full grid of placeholders; logical cells are placed by walking left-to-right
        # per row and skipping slots already occupied by a rowspan from above.
        grid = [['' for _ in range(n_cols)] for _ in range(n_rows)]

        cell_styles = [
            ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.black),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ]

        for r_idx, row in enumerate(rows):
            col_cursor = 0
            for cell in row:
                # Skip slots already filled by a rowspan from an earlier row.
                while col_cursor < n_cols and grid[r_idx][col_cursor] != '':
                    col_cursor += 1
                if col_cursor >= n_cols:
                    break

                colspan = cell.get("colspan", 1)
                rowspan = cell.get("rowspan", 1)
                font = "Helvetica-Bold" if cell.get("is_bold") else "Helvetica"
                sz = cell.get("size", 9.5)
                text = cell.get("text", "").replace("\n", " ") or " "
                style = ParagraphStyle(
                    name=f"TC_{r_idx}_{col_cursor}",
                    fontName=font,
                    fontSize=sz,
                    leading=sz * 1.2,
                    textColor=rl_colors.black,
                    spaceBefore=0,
                    spaceAfter=0,
                )
                grid[r_idx][col_cursor] = Paragraph(text, style)

                if colspan > 1 or rowspan > 1:
                    cell_styles.append(("SPAN",
                        (col_cursor, r_idx),
                        (col_cursor + colspan - 1, r_idx + rowspan - 1)))
                    # Mark every spanned slot with a sentinel so the rowspan
                    # skip-loop above knows they're occupied.
                    for dr in range(rowspan):
                        for dc in range(colspan):
                            if dr == 0 and dc == 0:
                                continue
                            if r_idx + dr < n_rows and col_cursor + dc < n_cols:
                                grid[r_idx + dr][col_cursor + dc] = ' '

                col_cursor += colspan

        if col_widths_pt and len(col_widths_pt) != n_cols:
            col_widths_pt = None

        t = Table(grid, colWidths=col_widths_pt, rowHeights=row_heights_pt)
        t.setStyle(TableStyle(cell_styles))

        available_w = elem.get("table_width_pt", pw)
        _, h = t.wrapOn(c, available_w, ph)
        t.drawOn(c, elem["x_pos"], ph - elem["y_pos"] - h)

    def _draw_image(self, c, elem: Dict[str, Any], ph: float):
        b64_data = elem.get("b64", "")
        if not b64_data:
            return
        img_reader = ImageReader(io.BytesIO(base64.b64decode(b64_data)))
        w = elem.get("width", 100)
        h = elem.get("height", 100)
        # y_pos = top of image in PDF coords; rl_y = bottom of image in RL coords
        c.drawImage(img_reader, elem["x_pos"], ph - elem["y1_pos"], width=w, height=h, mask="auto")

# ==========================================
# ENTRY POINT
# ==========================================

def extract_design_system(pdf_path: str, page_limit: int = 60) -> str:
    processor = PDFProcessor(pdf_path, page_limit)
    data = processor.run()
    return json.dumps(data, indent=2)

def generate_pdf(raw_json: str, output_path: str):
    data = json.loads(raw_json)
    renderer = ReportLabRenderer(data)
    renderer.render(output_path)

if __name__ == "__main__":
    PDF_INPUT = "sample.pdf"
    OUTPUT_PDF = "output.pdf"
    if not os.path.exists("test_results"):
        os.makedirs("test_results")

    try:
        logging.info("Starting extraction...")
        json_data = extract_design_system(PDF_INPUT)
        with open("test_results/sample.json", "w") as f:
            f.write(json_data)
        generate_pdf(json_data, OUTPUT_PDF)
        logging.info(f"Generated: {OUTPUT_PDF}")
    except Exception as e:
        logging.error(f"Failed: {e}")
        import traceback
        traceback.print_exc()