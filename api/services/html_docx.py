"""Editor HTML -> DOCX with formatting preserved.

Mirror image of docx_html.py: reads the inline CSS the importer (and the contentEditable
editor) produce and writes real Word formatting - run colour/size/font, paragraph alignment,
spacing, indent, shading, tables with widths / cell shading / borders / padding / merged
cells, and embedded pictures.
"""
import base64
import io
import re

import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from lxml import html as lhtml

_NAMED = {"white": "FFFFFF", "black": "000000", "red": "FF0000", "blue": "0000FF", "green": "008000",
          "gray": "808080", "grey": "808080", "yellow": "FFFF00", "orange": "FFA500", "navy": "000080"}
_ALIGN = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
          "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}
_PAGE_W_IN = 6.5  # usable width of the default Letter page with 1in margins
_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "li", "blockquote", "hr"}


def _css(el) -> dict:
    out = {}
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _pt(v):
    if v is None:
        return None
    m = re.match(r"^(-?[\d.]+)\s*(pt|px|em|rem|%)?$", v.strip())
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "pt"
    return {"pt": n, "px": n * 0.75, "em": n * 12, "rem": n * 12, "%": None}[unit]


def _hex(v):
    if not v:
        return None
    v = v.strip().lower()
    if v in _NAMED:
        return _NAMED[v]
    m = re.match(r"^#([0-9a-f]{6})$", v)
    if m:
        return m.group(1).upper()
    m = re.match(r"^#([0-9a-f])([0-9a-f])([0-9a-f])$", v)
    if m:
        return "".join(c * 2 for c in m.groups()).upper()
    m = re.match(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", v)
    if m:
        return "".join(f"{int(x):02X}" for x in m.groups())
    return None


def _text(s: str) -> str:
    s = s.replace("\u200b", "")
    s = re.sub(r"[ \t\r\n]+", " ", s)
    return s.replace("\u00a0", " ").replace("\u2003", "\t")


def _data_image(src: str):
    m = re.match(r"^data:image/[^;]+;base64,(.+)$", src or "", re.S)
    if not m:
        return None
    try:
        return io.BytesIO(base64.b64decode(m.group(1)))
    except Exception:
        return None


# ── inline ────────────────────────────────────────────────────────────────────

def _apply_run(run, fmt):
    f = run.font
    if fmt.get("bold"):
        f.bold = True
    if fmt.get("italic"):
        f.italic = True
    if fmt.get("underline"):
        f.underline = True
    if fmt.get("strike"):
        f.strike = True
    if fmt.get("sub"):
        f.subscript = True
    if fmt.get("sup"):
        f.superscript = True
    if fmt.get("color"):
        f.color.rgb = RGBColor.from_string(fmt["color"])
    if fmt.get("size"):
        f.size = Pt(fmt["size"])
    if fmt.get("font"):
        f.name = fmt["font"]
        rpr = run._r.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is not None:
            rfonts.set(qn("w:eastAsia"), fmt["font"])
            rfonts.set(qn("w:cs"), fmt["font"])
    if fmt.get("bg"):
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), fmt["bg"])
        run._r.get_or_add_rPr().append(shd)
    if fmt.get("caps"):
        f.all_caps = True


def _fmt_from_el(el, fmt):
    new = dict(fmt)
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag in ("strong", "b"):
        new["bold"] = True
    elif tag in ("em", "i"):
        new["italic"] = True
    elif tag == "u":
        new["underline"] = True
    elif tag in ("s", "strike", "del"):
        new["strike"] = True
    elif tag == "sub":
        new["sub"] = True
    elif tag == "sup":
        new["sup"] = True
    elif tag == "font":
        if _hex(el.get("color")):
            new["color"] = _hex(el.get("color"))
        if el.get("face"):
            new["font"] = el.get("face").split(",")[0].strip("'\" ")
    css = _css(el)
    if _hex(css.get("color")):
        new["color"] = _hex(css["color"])
    sz = _pt(css.get("font-size", ""))
    if sz:
        new["size"] = sz
    ff = css.get("font-family")
    if ff:
        new["font"] = ff.split(",")[0].strip("'\" ")
    bg = _hex(css.get("background-color") or css.get("background"))
    if bg and tag in ("span", "mark", "font"):
        new["bg"] = bg
    fw = css.get("font-weight", "")
    if fw in ("bold", "bolder") or (fw.isdigit() and int(fw) >= 600):
        new["bold"] = True
    if css.get("font-style") == "italic":
        new["italic"] = True
    td = css.get("text-decoration", "")
    if "underline" in td:
        new["underline"] = True
    if "line-through" in td:
        new["strike"] = True
    if css.get("text-transform") == "uppercase":
        new["caps"] = True
    return new


def _add_inline(el, par, fmt):
    """Append el's text/children as runs to par."""
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag == "br":
        par.add_run().add_break()
        return
    if tag == "img":
        stream = _data_image(el.get("src"))
        if stream:
            try:
                run = par.add_run()
                css = _css(el)
                w = _pt(css.get("width", ""))
                shape = run.add_picture(stream)
                target = Inches(min(w / 72, _PAGE_W_IN)) if w else min(shape.width, Inches(_PAGE_W_IN))
                if target and shape.width:
                    shape.height = int(shape.height * target / shape.width)
                    shape.width = int(target)
            except Exception:
                pass
        return
    new = _fmt_from_el(el, fmt) if tag else fmt
    if el.text and tag not in ("script", "style"):
        _emit_text(el.text, par, new)
    for ch in el:
        _add_inline(ch, par, new)
        if ch.tail:
            _emit_text(ch.tail, par, new)


def _emit_text(txt, par, fmt):
    t = _text(txt)
    if not t:
        return
    parts = t.split("\t")
    for i, seg in enumerate(parts):
        if i > 0:
            run = par.add_run()
            run.add_tab()
            _apply_run(run, fmt)
        if seg:
            run = par.add_run(seg)
            _apply_run(run, fmt)


# ── paragraph ─────────────────────────────────────────────────────────────────

def _border_xml(css_val):
    """'0.5pt solid #D0D7DE' | 'none' -> (val, sz, color)."""
    if not css_val or css_val.strip() == "none":
        return "nil", 0, "auto"
    m = re.match(r"^([\d.]+)(pt|px)?\s+(\w+)\s+(.+)$", css_val.strip())
    if not m:
        return "single", 4, "auto"
    w = float(m.group(1)) * (0.75 if m.group(2) == "px" else 1)
    kind = {"solid": "single", "double": "double", "dashed": "dashed", "dotted": "dotted"}.get(m.group(3), "single")
    return kind, max(2, min(96, round(w * 8))), _hex(m.group(4)) or "auto"


def _style_paragraph(par, css, keep_default_spacing=False):
    pf = par.paragraph_format
    al = css.get("text-align")
    if al in _ALIGN:
        pf.alignment = _ALIGN[al]
    mt, mb = _pt(css.get("margin-top", "")), _pt(css.get("margin-bottom", ""))
    if mt is not None:
        pf.space_before = Pt(max(mt, 0))
    if mb is not None:
        pf.space_after = Pt(max(mb, 0))
    ml, ti = _pt(css.get("margin-left", "")), _pt(css.get("text-indent", ""))
    if ml:
        pf.left_indent = Pt(ml)
    if ti:
        pf.first_line_indent = Pt(ti)
    lh = css.get("line-height")
    if lh:
        if re.match(r"^[\d.]+$", lh):
            pf.line_spacing = max(0.8, round(float(lh) / 1.17, 2)) if float(lh) < 3 else 1.0
        else:
            v = _pt(lh)
            if v:
                pf.line_spacing = Pt(v)
    bg = _hex(css.get("background-color"))
    ppr = par._p.get_or_add_pPr()
    sides = [s for s in ("top", "left", "bottom", "right") if f"border-{s}" in css]
    if sides:
        bdr = OxmlElement("w:pBdr")
        for s in sides:
            val, sz, color = _border_xml(css[f"border-{s}"])
            e = OxmlElement(f"w:{s}")
            e.set(qn("w:val"), val)
            e.set(qn("w:sz"), str(sz))
            e.set(qn("w:space"), "4")
            e.set(qn("w:color"), color)
            bdr.append(e)
        ppr.append(bdr)
    if bg:
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), bg)
        ppr.append(shd)


def _has_run_size(el) -> bool:
    return any("font-size" in (e.get("style") or "") for e in el.iter() if isinstance(e.tag, str))


def _make_paragraph(el, container, doc, first_par=None):
    tag = el.tag
    par = first_par if first_par is not None else container.add_paragraph()
    css = _css(el)
    if tag in ("h1", "h2", "h3") and not _has_run_size(el) and not (el.get("style") and "margin" in el.get("style")):
        level = {"h1": "Title", "h2": "Heading 1", "h3": "Heading 2"}[tag]
        try:
            par.style = doc.styles[level]
        except KeyError:
            pass
    _style_paragraph(par, css)
    _add_inline(el, par, {})
    return par


# ── tables ────────────────────────────────────────────────────────────────────

def _set_cell_xml(cell, css, ncols_pct=None):
    tcpr = cell._tc.get_or_add_tcPr()
    bg = _hex(css.get("background-color"))
    if bg:
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), bg)
        tcpr.append(shd)
    sides = [s for s in ("top", "left", "bottom", "right") if f"border-{s}" in css]
    if sides:
        b = OxmlElement("w:tcBorders")
        for s in sides:
            val, sz, color = _border_xml(css[f"border-{s}"])
            e = OxmlElement(f"w:{s}")
            e.set(qn("w:val"), val)
            e.set(qn("w:sz"), str(sz))
            e.set(qn("w:space"), "0")
            e.set(qn("w:color"), color)
            b.append(e)
        tcpr.append(b)
    pad = css.get("padding")
    if pad:
        vals = [_pt(x) for x in pad.split()]
        if vals and all(v is not None for v in vals):
            if len(vals) == 1:
                vals *= 4
            elif len(vals) == 2:
                vals = [vals[0], vals[1], vals[0], vals[1]]
            elif len(vals) == 3:
                vals = [vals[0], vals[1], vals[2], vals[1]]
            mar = OxmlElement("w:tcMar")
            for name, v in zip(("top", "right", "bottom", "left"), vals):
                e = OxmlElement(f"w:{name}")
                e.set(qn("w:w"), str(int(v * 20)))
                e.set(qn("w:type"), "dxa")
                mar.append(e)
            tcpr.append(mar)
    va = css.get("vertical-align")
    if va in ("middle", "bottom"):
        e = OxmlElement("w:vAlign")
        e.set(qn("w:val"), "center" if va == "middle" else "bottom")
        tcpr.append(e)


def _add_table(tbl_el, container, doc):
    # only rows that belong to this table (not nested)
    rows_el = [r for r in tbl_el.iter("tr") if _owner_table(r) is tbl_el]
    if not rows_el:
        return
    # grid placement
    grid = {}
    placements = []
    ncols = 0
    for ri, tr in enumerate(rows_el):
        ci = 0
        for td in [c for c in tr if c.tag in ("td", "th")]:
            while (ri, ci) in grid:
                ci += 1
            try:
                cs = max(1, int(td.get("colspan") or 1))
                rs = max(1, int(td.get("rowspan") or 1))
            except ValueError:
                cs = rs = 1
            for dr in range(rs):
                for dc in range(cs):
                    grid[(ri + dr, ci + dc)] = True
            placements.append((ri, ci, cs, rs, td))
            ci += cs
        ncols = max(ncols, ci)
    nrows = len(rows_el)
    if not ncols:
        return
    table = container.add_table(rows=nrows, cols=ncols)
    table.autofit = False
    tcss = _css(tbl_el)
    has_borders = any("border-" in (td.get("style") or "") or "border:" in (td.get("style") or "")
                      for _, _, _, _, td in placements)
    if not has_borders:
        try:
            table.style = doc.styles["Table Grid"]
        except KeyError:
            pass
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # column widths from <colgroup> percentages
    pcts = []
    for col in tbl_el.iter("col"):
        m = re.match(r"^([\d.]+)%$", _css(col).get("width", ""))
        pcts.append(float(m.group(1)) if m else None)
    page_w = _PAGE_W_IN
    if len(pcts) == ncols and all(p is not None for p in pcts):
        widths = [Inches(page_w * p / 100) for p in pcts]
        grid_cols = table._tbl.tblGrid.findall(qn("w:gridCol"))
        for gc, w in zip(grid_cols, widths):
            gc.set(qn("w:w"), str(int(w / 635)))
        for r in table.rows:
            for ci, c in enumerate(r.cells):
                c.width = widths[ci]

    # merges first, then content
    for ri, ci, cs, rs, td in placements:
        if cs > 1 or rs > 1:
            try:
                table.cell(ri, ci).merge(table.cell(ri + rs - 1, ci + cs - 1))
            except Exception:
                pass
    for ri, ci, cs, rs, td in placements:
        cell = table.cell(ri, ci)
        _set_cell_xml(cell, _css(td))
        _fill_container(td, cell, doc, first_par=cell.paragraphs[0])
        if td.tag == "th" and not _css(td).get("font-weight"):
            for p in cell.paragraphs:
                for r in p.runs:
                    if r.font.bold is None:
                        r.font.bold = True
    # Word needs a paragraph after a table that ends a cell/body
    return table


def _owner_table(tr):
    p = tr.getparent()
    while p is not None and p.tag != "table":
        p = p.getparent()
    return p


# ── containers ────────────────────────────────────────────────────────────────

def _fill_container(el, container, doc, first_par=None):
    """Convert el's mixed block/inline content into paragraphs/tables inside container."""
    state = {"par": first_par}

    def cur():
        if state["par"] is None:
            state["par"] = container.add_paragraph()
        return state["par"]

    def reusable():
        par = state["par"]
        return par if (par is not None and not par.runs) else None

    def text(t):
        if t and t.strip():
            _emit_text(t, cur(), {})

    text(el.text)
    for ch in el:
        tag = ch.tag if isinstance(ch.tag, str) else ""
        if tag == "table":
            _add_table(ch, container, doc)
            state["par"] = None
        elif tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li"):
            _make_paragraph(ch, container, doc, first_par=reusable())
            state["par"] = None
        elif tag in ("div", "ul", "ol"):
            if _is_pagebreak(ch):
                if container is doc:
                    doc.add_page_break()
                state["par"] = None
            elif any(isinstance(c.tag, str) and c.tag in _BLOCK_TAGS for c in ch):
                _fill_container(ch, container, doc, first_par=reusable())
                state["par"] = None
            else:
                _make_paragraph(ch, container, doc, first_par=reusable())
                state["par"] = None
        elif tag == "hr":
            pass
        elif tag == "br":
            cur().add_run().add_break()
        else:
            _add_inline(ch, cur(), {})
        text(ch.tail)


def _is_pagebreak(el) -> bool:
    return "ed-page-break" in (el.get("class") or "") or el.get("data-page-break") == "true"


def build_docx(content_html: str):
    """Return a python-docx Document built from editor HTML (suggestion <del> blocks dropped)."""
    cleaned = re.sub(r"<del\b[^>]*>.*?</del>", "", content_html or "", flags=re.DOTALL | re.IGNORECASE)
    root = lhtml.fragment_fromstring(f"<div>{cleaned}</div>")
    doc = docx.Document()
    # the default template ships one empty paragraph in some versions; keep body clean
    for p in list(doc.paragraphs):
        if not p.text.strip():
            p._p.getparent().remove(p._p)
    _fill_container(root, doc, doc)
    return doc
