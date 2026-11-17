"""High-fidelity DOCX -> editor HTML helpers.

Carries over what the original Word file actually specifies (run colour / size / font,
paragraph alignment / spacing / indent / shading, table widths, cell shading, borders,
padding, merged cells, nested tables and embedded pictures) as inline CSS so the editor
shows the document the way it looks in Word.
"""
import base64
import html
import io
import re

from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

IMG_MAX_COUNT = 12
IMG_MAX_B64_CHARS = 3_000_000
IMG_BUDGET = {"n": 0, "b64": 0}

_HIGHLIGHT = {
    "YELLOW": "#ffff00", "BRIGHT_GREEN": "#00ff00", "TURQUOISE": "#00ffff", "PINK": "#ff00ff",
    "BLUE": "#0000ff", "RED": "#ff0000", "DARK_BLUE": "#000080", "TEAL": "#008080",
    "GREEN": "#008000", "VIOLET": "#800080", "DARK_RED": "#800000", "DARK_YELLOW": "#808000",
    "GRAY_50": "#808080", "GRAY_25": "#c0c0c0", "BLACK": "#000000", "WHITE": "#ffffff",
}


def reset_budget() -> None:
    IMG_BUDGET.update({"n": 0, "b64": 0})


# ── images ────────────────────────────────────────────────────────────────────

def images_html(p_el, part) -> str:
    """Inline <img> tags for DrawingML (a:blip) and VML (v:imagedata) pictures under p_el."""
    rids = p_el.xpath('.//*[local-name()="blip"]/@*[local-name()="embed"]')
    rids += p_el.xpath('.//*[local-name()="imagedata"]/@*[local-name()="id"]')
    out, seen = [], set()
    for rid in rids:
        if rid in seen:
            continue
        seen.add(rid)
        try:
            img_part = part.related_parts[rid]
            blob = img_part.blob
            ctype = (img_part.content_type or "").lower()
        except Exception:
            continue
        if not blob or "emf" in ctype or "wmf" in ctype:
            continue
        if IMG_BUDGET["n"] >= IMG_MAX_COUNT:
            break
        try:
            if len(blob) > 250_000 or ctype not in ("image/png", "image/jpeg", "image/jpg", "image/gif"):
                from PIL import Image
                im = Image.open(io.BytesIO(blob))
                im.thumbnail((900, 900))
                buf = io.BytesIO()
                if im.mode in ("RGBA", "LA", "P"):
                    im.convert("RGBA").save(buf, "PNG", optimize=True)
                    ctype = "image/png"
                else:
                    im.convert("RGB").save(buf, "JPEG", quality=70)
                    ctype = "image/jpeg"
                blob = buf.getvalue()
        except Exception:
            if len(blob) > 250_000:
                continue
        b64 = base64.b64encode(blob).decode()
        if IMG_BUDGET["b64"] + len(b64) > IMG_MAX_B64_CHARS:
            break
        IMG_BUDGET["n"] += 1
        IMG_BUDGET["b64"] += len(b64)
        # honour the picture's authored size when present (EMU -> px at 96dpi)
        size = ""
        ext = None
        for cx in p_el.xpath('.//*[local-name()="extent"]'):
            ext = cx
            break
        if ext is not None:
            try:
                w_px = round(int(ext.get("cx")) / 9525)
                size = f"width:{w_px}px;"
            except Exception:
                pass
        out.append(f'<img src="data:{ctype};base64,{b64}" alt="" style="{size}max-width:100%;height:auto;"/>')
    return "".join(out)


def textbox_lines(p_el) -> list:
    lines = []
    for tb in p_el.xpath('.//*[local-name()="txbxContent"][not(ancestor::*[local-name()="Fallback"])]'):
        for sub in tb.xpath('.//*[local-name()="p"]'):
            t = "".join(sub.xpath('.//*[local-name()="t"]/text()')).strip()
            if t:
                lines.append(t)
    return lines


# ── style resolution ──────────────────────────────────────────────────────────

def _chain(style):
    seen = 0
    while style is not None and seen < 12:
        yield style
        style = style.base_style
        seen += 1


def _eff_font(run, p, attr):
    v = getattr(run.font, attr)
    if v is not None:
        return v
    try:
        for st in _chain(run.style):
            v = getattr(st.font, attr)
            if v is not None:
                return v
    except Exception:
        pass
    try:
        for st in _chain(p.style):
            v = getattr(st.font, attr)
            if v is not None:
                return v
    except Exception:
        pass
    return None


def _eff_color(run, p):
    def rgb_of(font):
        try:
            if font.color is not None and font.color.rgb is not None:
                return str(font.color.rgb)
        except Exception:
            pass
        return None

    c = rgb_of(run.font)
    if c:
        return c
    try:
        for st in _chain(run.style):
            c = rgb_of(st.font)
            if c:
                return c
    except Exception:
        pass
    try:
        for st in _chain(p.style):
            c = rgb_of(st.font)
            if c:
                return c
    except Exception:
        pass
    return None


def _pt(v) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def run_css(run, p) -> str:
    css = []
    c = _eff_color(run, p)
    if c and c.upper() != "000000":
        css.append(f"color:#{c}")
    size = _eff_font(run, p, "size")
    if size is not None:
        css.append(f"font-size:{_pt(size.pt)}pt")
    name = _eff_font(run, p, "name")
    if name:
        css.append(f"font-family:'{name}',sans-serif")
    try:
        hl = run.font.highlight_color
        if hl is not None and hl.name in _HIGHLIGHT:
            css.append(f"background-color:{_HIGHLIGHT[hl.name]}")
    except Exception:
        pass
    shd = run._r.xpath("./w:rPr/w:shd/@w:fill")
    if shd and shd[0].lower() not in ("auto", "ffffff"):
        css.append(f"background-color:#{shd[0]}")
    deco = []
    if _eff_font(run, p, "strike"):
        deco.append("line-through")
    if deco:
        css.append("text-decoration:" + " ".join(deco))
    if _eff_font(run, p, "all_caps"):
        css.append("text-transform:uppercase")
    if _eff_font(run, p, "small_caps"):
        css.append("font-variant:small-caps")
    sp = run._r.xpath("./w:rPr/w:spacing/@w:val")
    if sp:
        try:
            sp_pt = int(sp[0]) / 20
            if abs(sp_pt) >= 0.5:
                css.append(f"letter-spacing:{_pt(sp_pt)}pt")
        except Exception:
            pass
    return ";".join(css)


def _wrap_run_group(txt: str, sig: tuple) -> str:
    bold, italic, underline, sub, sup, css = sig
    if bold:
        txt = f"<strong>{txt}</strong>"
    if italic:
        txt = f"<em>{txt}</em>"
    if underline:
        txt = f"<u>{txt}</u>"
    if sub:
        txt = f"<sub>{txt}</sub>"
    if sup:
        txt = f"<sup>{txt}</sup>"
    if css:
        txt = f'<span style="{css}">{txt}</span>'
    return txt


def runs_html(p) -> str:
    """Paragraph runs -> HTML (merges adjacent runs with identical styling, preserves inline pictures)."""
    parts = []
    cur_sig = None
    cur_buf = []

    def _flush():
        nonlocal cur_sig, cur_buf
        if cur_buf and cur_sig is not None:
            parts.append(_wrap_run_group("".join(cur_buf), cur_sig))
        cur_sig = None
        cur_buf = []

    run_els = p._p.xpath(
        "./w:r | ./w:hyperlink/w:r | ./w:ins/w:r | ./w:smartTag/w:r | ./w:sdt/w:sdtContent/w:r"
    )
    for el in run_els:
        r = Run(el, p)
        txt = r.text or ""
        br_count = len(el.xpath("./w:br[not(@w:type) or @w:type='textWrapping']"))
        if txt or br_count:
            escaped = html.escape(txt).replace("\t", "&emsp;").replace("\n", "<br/>")
            if br_count and not txt.endswith("\n"):
                escaped += "<br/>" * br_count
            sig = (
                bool(_eff_font(r, p, "bold")),
                bool(_eff_font(r, p, "italic")),
                bool(r.underline or _eff_font(r, p, "underline")),
                bool(_eff_font(r, p, "subscript")),
                bool(_eff_font(r, p, "superscript")),
                run_css(r, p),
            )
            if cur_sig is not None and (sig == cur_sig or not txt.strip()):
                cur_buf.append(escaped)
            else:
                _flush()
                cur_sig = sig
                cur_buf = [escaped]
        img = images_html(el, p.part)
        if img:
            _flush()
            parts.append(img)
    _flush()
    res = "".join(parts).strip()
    return res or html.escape(p.text.strip())


def _border_css(el) -> str:
    if el is None:
        return ""
    val = el.get(qn("w:val"))
    if val in (None, "nil", "none"):
        return "none"
    try:
        sz = max(int(el.get(qn("w:sz")) or 4) / 8, 0.5)
    except Exception:
        sz = 0.5
    color = el.get(qn("w:color")) or "auto"
    color = "#000000" if color == "auto" else f"#{color}"
    kind = {"double": "double", "dashed": "dashed", "dotted": "dotted", "dashSmallGap": "dashed",
            "dotDash": "dashed", "thick": "solid"}.get(val, "solid")
    if kind == "double":
        sz = max(sz, 3)
    return f"{_pt(sz)}pt {kind} {color}"


def _pf_value(p, attr):
    v = getattr(p.paragraph_format, attr)
    if v is not None:
        return v
    try:
        for st in _chain(p.style):
            v = getattr(st.paragraph_format, attr)
            if v is not None:
                return v
    except Exception:
        pass
    return None


def para_css(p) -> dict:
    """Paragraph-level CSS as an ordered dict (alignment, spacing, indent, shading, borders)."""
    css = {}
    al = p.alignment
    if al is None:
        al = _pf_value(p, "alignment")
    name = None
    if al is not None:
        name = {0: "left", 1: "center", 2: "right", 3: "justify"}.get(int(al))
    if name:
        css["text-align"] = name
    sb, sa = _pf_value(p, "space_before"), _pf_value(p, "space_after")
    if sb is not None:
        css["margin-top"] = f"{_pt(sb.pt)}pt"
    if sa is not None:
        css["margin-bottom"] = f"{_pt(sa.pt)}pt"
    ls = _pf_value(p, "line_spacing")
    if ls is not None:
        if hasattr(ls, "pt"):
            css["line-height"] = f"{_pt(ls.pt)}pt"
        else:
            css["line-height"] = _pt(float(ls) * 1.17) if float(ls) <= 3 else "normal"
    li, fi = _pf_value(p, "left_indent"), _pf_value(p, "first_line_indent")
    if li is not None and li.pt:
        css["margin-left"] = f"{_pt(li.pt)}pt"
    if fi is not None and fi.pt:
        css["text-indent"] = f"{_pt(fi.pt)}pt"
    shd = p._p.xpath("./w:pPr/w:shd/@w:fill")
    if shd and shd[0].lower() not in ("auto",):
        css["background-color"] = f"#{shd[0]}"
    for side in ("top", "bottom", "left", "right"):
        els = p._p.xpath(f"./w:pPr/w:pBdr/w:{side}")
        if els:
            b = _border_css(els[0])
            if b:
                css[f"border-{side}"] = b
                if side in ("left", "right"):
                    css[f"padding-{side}"] = "6px"
    return css


def style_attr(css: dict, defaults: dict = None) -> str:
    merged = dict(defaults or {})
    merged.update(css)
    if not merged:
        return ""
    return ' style="' + ";".join(f"{k}:{v}" for k, v in merged.items()) + '"'


# ── tables ────────────────────────────────────────────────────────────────────

def _table_style_borders(tbl) -> dict:
    out = {}
    try:
        for st in _chain(tbl.style):
            for el in st.element.xpath("./w:tblPr/w:tblBorders/*"):
                side = el.tag.split("}")[-1]
                out.setdefault(side, el)
    except Exception:
        pass
    return out


def _cell_html(tc, parent, nested_ctx=None) -> str:
    parts = []
    paras = []
    for child in tc.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            paras.append(("p", child))
        elif tag == "tbl":
            paras.append(("tbl", child))
    # drop trailing empty paragraphs (Word's mandatory last <w:p/>)
    while paras and paras[-1][0] == "p" and not "".join(paras[-1][1].xpath(".//w:t/text()")).strip() \
            and not images_html(paras[-1][1], parent.part):
        paras.pop()
    for kind, el in paras:
        if kind == "tbl":
            parts.append(table_to_html(Table(el, parent)))
            continue
        p = Paragraph(el, parent)
        css = para_css(p)
        css.setdefault("margin-top", "0")
        css.setdefault("margin-bottom", "0")
        css.setdefault("line-height", "1.35")
        inner = runs_html(p) if (p.text.strip() or images_html(el, p.part)) else "<br/>"
        parts.append(f'<p class="ed-doc-cellp"{style_attr(css)}>{inner}</p>')
    return "".join(parts)


def table_to_html(
    tbl,
    bilingual_order: list[str] | None = None,
    borderless: bool = False,
    page_char_budget: int = 0,
) -> str:
    tblEl = tbl._tbl
    grid = []
    for g in tblEl.xpath("./w:tblGrid/w:gridCol"):
        try:
            grid.append(int(g.get(qn("w:w"))))
        except Exception:
            grid.append(0)

    # table-level borders (direct beats style)
    borders = {}
    for side, el in _table_style_borders(tbl).items():
        borders[side] = _border_css(el)
    for el in tblEl.xpath("./w:tblPr/w:tblBorders/*"):
        borders[el.tag.split("}")[-1]] = _border_css(el)
    cell_mar = {"top": 0, "bottom": 0, "left": 5.4, "right": 5.4}
    try:
        for st in _chain(tbl.style):
            for el in st.element.xpath("./w:tblPr/w:tblCellMar/*"):
                s = el.tag.split("}")[-1].replace("start", "left").replace("end", "right")
                if el.get(qn("w:w")):
                    cell_mar[s] = int(el.get(qn("w:w"))) / 20
    except Exception:
        pass
    for el in tblEl.xpath("./w:tblPr/w:tblCellMar/*"):
        s = el.tag.split("}")[-1].replace("start", "left").replace("end", "right")
        if el.get(qn("w:w")):
            cell_mar[s] = int(el.get(qn("w:w"))) / 20

    # rows -> cells with grid positions, gridSpan and vMerge
    rows = []
    open_vm = {}
    for tr in tblEl.xpath("./w:tr"):
        row, col = [], 0
        for tc in tr.xpath("./w:tc"):
            try:
                span = int((tc.xpath("./w:tcPr/w:gridSpan/@w:val") or ["1"])[0])
            except Exception:
                span = 1
            vm = tc.xpath("./w:tcPr/w:vMerge")
            cell = {"tc": tc, "col": col, "span": span, "rowspan": 1}
            if vm:
                v = vm[0].get(qn("w:val"))
                if v == "restart":
                    open_vm[col] = cell
                    row.append(cell)
                elif col in open_vm:
                    open_vm[col]["rowspan"] += 1
                else:
                    row.append(cell)
            else:
                open_vm.pop(col, None)
                row.append(cell)
            col += span
        rows.append(row)
    if not rows:
        return ""
    ncols = max([c["col"] + c["span"] for r in rows for c in r] + [len(grid), 1])

    # column widths: prefer authored cell widths (first fully specified row), else grid
    widths = None
    for r in rows:
        ws, ok = [], True
        for c in r:
            tcw = c["tc"].xpath("./w:tcPr/w:tcW")
            if tcw and tcw[0].get(qn("w:type")) == "dxa" and tcw[0].get(qn("w:w")):
                ws.append((c, int(tcw[0].get(qn("w:w")))))
            else:
                ok = False
                break
        if ok and ws and sum(c["span"] for c, _ in ws) == ncols:
            widths = [0] * ncols
            for c, w in ws:
                for k in range(c["span"]):
                    widths[c["col"] + k] = w / c["span"]
            break
    if not widths and grid and len(grid) == ncols and sum(grid) > 0:
        widths = grid
    total = sum(widths) if widths else 0

    classes = ["ed-doc-table"]
    extra_attrs = []
    if bilingual_order and bilingual_order in (["en", "id"], ["id", "en"]):
        classes.append("bilingual-contract-table")
        extra_attrs.append(f'data-column-order="{bilingual_order[0]},{bilingual_order[1]}"')
    else:
        bilingual_order = None
    if borderless:
        if bilingual_order:
            classes.append("is-borderless")
        extra_attrs.append('data-borderless="true"')
    attr_str = f' {" ".join(extra_attrs)}' if extra_attrs else ""
    table_open_parts = [f'<table class="{" ".join(classes)}"{attr_str}>']
    if total:
        table_open_parts.append("<colgroup>" + "".join(
            f'<col style="width:{w / total * 100:.2f}%"/>' for w in widths) + "</colgroup>")
    table_open_parts.append("<tbody>")
    table_open = "".join(table_open_parts)
    out = [table_open]
    nrows = len(rows)
    running_chars = 0
    open_rowspan_until = 0
    for ri, r in enumerate(rows):
        row_has_break = any(
            c["tc"].xpath(".//w:br[@w:type='page'] | .//w:pPr/w:pageBreakBefore")
            for c in r
        )
        if (
            page_char_budget > 0
            and ri > 0
            and ri >= open_rowspan_until
            and (row_has_break or running_chars >= page_char_budget)
        ):
            out.append("</tbody></table>")
            auto_attr = "" if row_has_break else ' data-auto-break="true"'
            out.append(
                f'<div class="ed-page-break" contenteditable="false" role="separator" '
                f'aria-label="Page Break" data-page-break="true"{auto_attr}></div>'
            )
            out.append(table_open)
            running_chars = 0

        if bilingual_order and len(r) == 1 and r[0]["span"] >= 2:
            out.append('<tr class="bilingual-section-row">')
        elif bilingual_order and len(r) == 2:
            out.append('<tr class="bilingual-clause-row">')
        else:
            out.append("<tr>")
        row_chars = 0
        for cell_idx, c in enumerate(r):
            if c["rowspan"] > 1:
                open_rowspan_until = max(open_rowspan_until, ri + c["rowspan"])
            row_chars = max(row_chars, len("".join(c["tc"].itertext()).strip()))
            tc = c["tc"]
            css = []
            shd = tc.xpath("./w:tcPr/w:shd/@w:fill")
            if shd and shd[0].lower() != "auto":
                css.append(f"background-color:#{shd[0]}")
            # borders
            tcb = {el.tag.split("}")[-1]: _border_css(el) for el in tc.xpath("./w:tcPr/w:tcBorders/*")}
            first_row, last_row = ri == 0, (ri + c["rowspan"] >= nrows)
            first_col, last_col = c["col"] == 0, (c["col"] + c["span"] >= ncols)
            sides = {
                "top": tcb.get("top") or borders.get("top" if first_row else "insideH"),
                "bottom": tcb.get("bottom") or borders.get("bottom" if last_row else "insideH"),
                "left": tcb.get("left", tcb.get("start")) or borders.get("left" if first_col else "insideV"),
                "right": tcb.get("right", tcb.get("end")) or borders.get("right" if last_col else "insideV"),
            }
            for side, val in sides.items():
                css.append(f"border-{side}:{val or 'none'}")
            # padding
            mar = dict(cell_mar)
            for el in tc.xpath("./w:tcPr/w:tcMar/*"):
                s = el.tag.split("}")[-1].replace("start", "left").replace("end", "right")
                if el.get(qn("w:w")):
                    mar[s] = int(el.get(qn("w:w"))) / 20
            css.append(f"padding:{_pt(mar['top'])}pt {_pt(mar['right'])}pt {_pt(mar['bottom'])}pt {_pt(mar['left'])}pt")
            va = (tc.xpath("./w:tcPr/w:vAlign/@w:val") or [None])[0]
            css.append(f"vertical-align:{ {'center': 'middle', 'bottom': 'bottom'}.get(va, 'top') }")
            attrs = ""
            if bilingual_order and len(r) == 2 and c["span"] == 1:
                lang = bilingual_order[cell_idx]
                attrs += f' class="col-{lang}" lang="{lang}"'
            if c["span"] > 1:
                attrs += f' colspan="{c["span"]}"'
            if c["rowspan"] > 1:
                attrs += f' rowspan="{c["rowspan"]}"'
            out.append(f'<td{attrs} style="{";".join(css)}">{_cell_html(tc, tbl)}</td>')
        out.append("</tr>")
        running_chars += row_chars
    out.append("</tbody></table>")
    return "".join(out)

