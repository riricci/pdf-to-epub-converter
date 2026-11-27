#!/usr/bin/env python3
"""Scientific PDF (book, article in one or two columns, slide deck) -> EPUB3.

The document is profiled first (body size, note size, running heads/footers,
fonts, math fonts, columns, slides vs pages), so no per-book tuning is needed.
Display equations, figures and tables become images; running text is rebuilt
into paragraphs, inline formulas that cannot be text become small inline images,
footnotes become EPUB3 popup notes (noteref + aside).

Usage:
    python pdf2epub.py INPUT.pdf [MORE.pdf ...] OUTPUT.epub [--title T] [--authors "A;B"]
                       [--pages 59-86] [--report report.json] [--lang it]
Several PDFs (e.g. one per slide deck) become chapters of a single EPUB.
"""
import argparse
import collections
import difflib
import html
import json
import re
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field

import fitz  # PyMuPDF

MIN_FIG_W, MIN_FIG_H = 30, 20
EQ_ZOOM = 2.5
FIG_ZOOM = 2.0


class Profile:
    """Document-wide measurements, filled in by analyze(); defaults fit a 10pt LaTeX book."""
    body = 10.0            # body font size; image widths are expressed in em of this
    note = 9.0             # footnote text size
    text_fonts = set()     # font names of the running text
    running = set()        # normalised text of running heads / footers
    header_y = 0.0         # graphics above / below these are page furniture
    footer_y = 1e9
    slides = False
    lang = "it"


PF = Profile()
BODY_PT = PF.body          # kept in sync with PF.body by analyze()
COL = {"w": 335.0}         # width of the column being processed

# math fonts: TeX (CM*, AMS, txfonts/pxfonts, Latin Modern, newtx), Word (Cambria Math),
# MathType (MT Extra, Euclid), STIX/XITS, Symbol...
MATH_RE = re.compile(r"^(CMMI|CMSY|CMEX|CMBSY|MSBM|MSAM|CMR[5-8]\b|CMR[5-8]$|EUFM|EUSM|RSFS|"
                     r"rtxmi|rtxsy|rtxex|txmi|txsy|txex|pxmi|pxsy|pxex|NewTXMI|NewPXMI|ntxmi|ntxsy|"
                     r"LMMath|LMRoman[5-8]|MTMI|MTSY|MTEX|MT-?Extra|Euclid|Symbol|STIX|XITS|"
                     r"Cambria-?Math|CambriaMath|Asana|TeXGyre\w*Math|Latin ?Modern ?Math|"
                     r"NewCMMath|MathematicalPi|Mathematica|ESSTIX|Universal-?Greek|Fourier-?Math|"
                     r"msbm|msam|cmmi|cmsy|cmex)", re.I)
# fonts whose letters are symbols/variables (excludes the small roman CMR5-8 used for indices)
SYMBOLIC_RE = re.compile(r"^(CMMI|CMSY|CMEX|CMBSY|MSBM|MSAM|rtxmi|rtxsy|txmi|txsy|pxmi|pxsy|"
                         r"NewTXMI|ntxmi|LMMathItalic|LMMathSymbols|LMMathExtension|MTMI|MTSY|MTEX|"
                         r"Euclid|STIX\w*Math|XITSMath|Cambria-?Math|CambriaMath|Symbol)", re.I)
BIGOP_RE = re.compile(r"^(CMEX|LMMathExtension|MTEX|txex|rtxex|pxex|Euclid ?Extra|STIXSize|STIXIntegrals)", re.I)
ITALIC_RE = re.compile(r"(CMMI|CMTI|CMSL|Ital|Oblique|-It\b|-It$|BoldIt|LMRoman\d*-Italic|Slanted|rtxmi|MTMI)", re.I)
BOLD_RE = re.compile(r"(CMBX|CMMIB|CMBSY|Bold|-Bd|Semibold|Demi|Black|Heavy|-Sb\b)", re.I)
BANNED_FONTS = ("Comic",)    # never shown: redrawn in a sans serif inside images
_FONT_DIRS = ["/System/Library/Fonts/Supplemental", "/Library/Fonts", "/usr/share/fonts/truetype/dejavu",
              "/usr/share/fonts/TTF", "C:/Windows/Fonts"]


def _find_font(*names):
    import os
    for d in _FONT_DIRS:
        for n in names:
            if os.path.exists(os.path.join(d, n)):
                return os.path.join(d, n)
    return None


SANS = _find_font("Arial Unicode.ttf", "Arial.ttf", "arial.ttf", "DejaVuSans.ttf")
SANS_BOLD = _find_font("Arial Bold.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") or SANS
BULLETS = "•▪◦‣∙●○■□➢➤►▶✓✔–—-*·"


def base_font(f):
    return f.split("+", 1)[-1]


def font_family(f):
    """CMBX10 -> CMBX, Helvetica-BoldOblique -> Helvetica, ABCDEF+Times,Bold -> Times"""
    b = re.split(r"[-,]", base_font(f))[0]
    return re.sub(r"(MT|PS|Std|Pro|LT)?\d*$", "", b) or b


def is_math_font(f):
    return bool(MATH_RE.match(base_font(f)))


def is_symbolic(f):
    return bool(SYMBOLIC_RE.match(base_font(f)))


def is_math_char(c):
    o = ord(c[0]) if c else 0
    return (0x2200 <= o <= 0x22FF or 0x27C0 <= o <= 0x27EF or 0x2980 <= o <= 0x2AFF
            or 0x1D400 <= o <= 0x1D7FF or 0x0391 <= o <= 0x03C9 or 0x03D0 <= o <= 0x03F6
            or 0x2190 <= o <= 0x21FF or c in "∂∇√∞±∓×÷⋅∑∏∫")

ACCENTS = {"`": "\u0300", "´": "\u0301", "¨": "\u0308", "ˆ": "\u0302",
           "˜": "\u0303", "˙": "\u0307", "¯": "\u0304", "˚": "\u030a", "ˇ": "\u030c"}
LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}
EQNUM_RE = re.compile(r"^\(\s*([A-Z][.\-]?)?\d+([.\-]\d+)*[a-z']?\s*\)$|^\[\s*\d+([.\-]\d+)*[a-z]?\s*\]$")
PLACEHOLDER = "\ufffc"
CAPTION_RE = re.compile(r"^(Figura|Figure|FIGURE|Fig\.|FIG\.|Fig|Abb\.|Abbildung|Tabella|Table|TABLE|"
                        r"Tab\.|Tabelle|Tavola|Schema|Scheme|Grafico|Chart)\s*[A-Z]?[0-9IVX]+([.\-–][0-9]+)*[a-z]?"
                        r"(\s*[:|—–-]\s+|\.(?![\d)])\s*|\s+(?=[A-Z(]))")
TABLE_CAP_RE = re.compile(r"^(Tabella|Table|TABLE|Tab\.|Tabelle|Tavola)")
SKIP_SECTIONS = re.compile(r"^(Indice|Indice analitico|Sommario|Contents|Table of Contents|Index|"
                           r"Title Page|Titelei|Inhaltsverzeichnis|Table des matières)\s*$", re.I)


# ---------------------------------------------------------------- geometry
def rect_union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def touches(a, b, tol):
    return not (a[2] + tol < b[0] or b[2] + tol < a[0] or a[3] + tol < b[1] or b[3] + tol < a[1])


def merge_rects(rects, tol):
    """Union of rectangles that touch within `tol` (connected components)."""
    rects = [tuple(r) for r in rects]
    changed = True
    while changed:
        changed = False
        out = []
        while rects:
            cur = rects.pop()
            i = 0
            while i < len(rects):
                if touches(cur, rects[i], tol):
                    cur = rect_union(cur, rects.pop(i))
                    changed = True
                    i = 0
                else:
                    i += 1
            out.append(cur)
        rects = out
    return rects


def area_overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


# ---------------------------------------------------------------- text model
@dataclass
class Char:
    c: str
    bbox: tuple
    size: float
    font: str
    flags: int
    oy: float           # baseline y
    overline: bool = False


@dataclass
class Line:
    chars: list
    bbox: tuple

    @property
    def text(self):
        return "".join(ch.c for ch in self.chars)


@dataclass
class Row:
    lines: list
    bbox: tuple
    kind: str = "text"   # text | display

    @property
    def text(self):
        return " ".join(l.text for l in sorted(self.lines, key=lambda l: l.bbox[0]))


@dataclass
class Item:
    kind: str            # para | heading | img
    y0: float
    y1: float = 0
    rows: list = field(default_factory=list)
    level: int = 0
    html: str = ""
    page: int = 0
    title: str = ""
    region: int = 0
    left: float = 0
    right: float = 0


def run_key(text):
    """Normalised text used to recognise running heads/footers across pages."""
    t = unicodedata.normalize("NFKD", text).lower()
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"\b[ivxlc]+\b", "#", t) if len(t) < 12 else t
    return re.sub(r"[^a-z#]+", "", t)


def is_pagenum(text):
    t = text.strip().strip("\u2013\u2014-|\u00b7\u2022()[] ")
    return bool(re.fullmatch(r"\d{1,4}|[ivxlcIVXLC]{1,7}|(pag\.|page|p\.)\s*\d+(\s*(di|of|/)\s*\d+)?|\d+\s*/\s*\d+", t, re.I))


def in_band(bbox, page_h):
    return bbox[3] < 0.1 * page_h or bbox[1] > 0.9 * page_h


def raw_lines(page):
    """(bbox, text, spans) of every text line of the page."""
    out = []
    for b in page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_LIGATURES)["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            for sp in l["spans"]:
                sp["text"] = "".join(c["c"] for c in sp["chars"])
            out.append(l)
    return out


def page_lines(page, report, pno):
    """Text lines (char-level) without running heads/footers, invisible text and junk."""
    out = []
    h = page.rect.height
    for l in raw_lines(page):
        if l["dir"][0] < 0.9:          # rotated text (arXiv stamp, axis labels): figure material only
            continue
        chars = []
        for s in l["spans"]:
            if s["size"] < 1 or s.get("alpha", 255) == 0:
                continue
            wing = "Wingdings" in s["font"] or "Webdings" in s["font"]
            for ch in s["chars"]:
                c = ch["c"]
                if c == "\xad":
                    c = "-"            # soft hyphen drawn as a hyphen
                elif wing or 0xF000 <= ord(c) <= 0xF0FF:
                    c = c if c.isspace() else wingding(c, not chars or all(x.c.isspace() for x in chars))
                elif ord(c) < 32 or c == "\ufffd" or 0xD800 <= ord(c) <= 0xDFFF or \
                        (c in '!"#$' and is_symbolic(s["font"])):   # math glyphs without unicode (Cambria Math)
                    c = PLACEHOLDER  # unmapped glyph; forces display rendering
                chars.append(Char(c, tuple(ch["bbox"]), s["size"], s["font"],
                                  s["flags"], ch["origin"][1]))
        if chars and all(ch.c in '!"# ' + PLACEHOLDER for ch in chars) and \
                any("Cambria" in s["font"] and not is_math_font(s["font"]) for s in l["spans"]):
            continue      # stray unmapped glyph (punctuation) left by a Word/PowerPoint equation
        if chars and any(not ch.c.isspace() for ch in chars):
            ln = Line(chars, tuple(l["bbox"]))
            if ln.bbox[3] <= PF.header_y or ln.bbox[1] >= PF.footer_y or \
                    (ln.bbox[1] < PF.header_y and ln.bbox[3] < PF.header_y + 0.5 * PF.body) or \
                    (ln.bbox[3] > PF.footer_y and ln.bbox[1] >= PF.footer_y - 1):
                continue
            if in_band(ln.bbox, h):
                t = ln.text.strip()
                if is_pagenum(t) or run_key(t) in PF.running:
                    continue  # running head, footer, page number
            out.append(ln)
    return out


def wingding(c, at_start):
    """Wingdings/Webdings glyph -> Unicode: arrows stay arrows, the rest are bullets."""
    k = ord(c) & 0xFF
    if 0xD5 <= k <= 0xF0 and not (at_start and k == 0xD8):
        return "⇒" if not at_start else "➢ "
    if k in (0xFB, 0xFC, 0xFE):
        return "✓ " if at_start else "✓"
    return "• " if at_start else "•"


def analyze(doc, pages=None):
    """Measure the document: body/note size, text fonts, running heads, slides."""
    pages = list(pages if pages is not None else range(doc.page_count))
    sample = pages if len(pages) <= 400 else pages[::max(1, len(pages) // 400)]
    sizes, fonts = collections.Counter(), collections.Counter()
    keys = collections.Counter()
    land = 0
    band_lines = []
    for pno in sample:
        page = doc[pno]
        if page.rect.width > 1.15 * page.rect.height:
            land += 1
        seen = set()
        for l in raw_lines(page):
            txt = "".join(c["c"] for s in l["spans"] for c in s["chars"]).strip()
            if not txt:
                continue
            if in_band(l["bbox"], page.rect.height):
                k = run_key(txt)
                if k and k not in seen:
                    seen.add(k)
                    keys[k] += 1
                band_lines.append((pno, l["bbox"], k, page.rect.height, txt))
            prose = len(txt) >= 30
            for s in l["spans"]:
                n = sum(1 for c in s["chars"] if c["c"].strip())
                if n and not is_math_font(s["font"]):
                    sizes[round(s["size"] * 2) / 2] += n
                    if prose:
                        fonts[font_family(s["font"])] += n
    PF.slides = land > 0.6 * max(1, len(sample))
    if sizes:
        PF.body = sizes.most_common(1)[0][0]
    tot = sum(fonts.values()) or 1
    PF.text_fonts = {f for f, n in fonts.items() if n > 0.005 * tot}   # families of the prose
    n = len(sample)
    # running heads share the baseline of the page number; everything up to there is furniture
    tops, bots = [], []
    by_page = collections.defaultdict(list)
    for p, b, k, h, t in band_lines:
        by_page[p].append((b, k, h, t))
    for p, ls in by_page.items():
        for b, k, h, t in ls:
            if not is_pagenum(t):
                continue
            same = [o for o in ls if abs(o[0][1] - b[1]) < 2.5 and abs(o[0][3] - b[3]) < 2.5]
            if b[3] < 0.1 * h:
                tops.append(max(o[0][3] for o in same))
            else:
                bots.append(min(o[0][1] for o in same))
    PF.header_y = sorted(tops)[int(0.9 * (len(tops) - 1))] + 0.5 if len(tops) >= max(2, 0.25 * n) else 0
    PF.footer_y = sorted(bots)[int(0.1 * (len(bots) - 1))] - 0.5 if len(bots) >= max(2, 0.25 * n) else 1e9
    # repeated lines in the margins (journal footers, copyright lines, slide footers)
    top_k = collections.Counter(k for p, b, k, h, t in band_lines if b[3] < 0.1 * h)
    bot_k = collections.Counter(k for p, b, k, h, t in band_lines if b[1] > 0.9 * h)
    if n > 6:
        PF.running = {k for k, c in top_k.items() if c >= max(3, 0.12 * n)} | \
                     {k for k, c in bot_k.items() if c >= max(3, 0.02 * n)}
    else:
        PF.running = {k for k, c in (top_k + bot_k).items() if c >= max(2, 0.5 * n)}
    PF.running = {k for k in PF.running if len(k) > 1}
    # footnote size: text that follows a small raised number at the bottom of pages
    notes = collections.Counter()
    for pno in sample:
        page = doc[pno]
        for l in raw_lines(page):
            if l["bbox"][1] < 0.5 * page.rect.height:
                continue
            chs = [(c["c"], s["size"], c["origin"][1]) for s in l["spans"] for c in s["chars"] if c["c"].strip()]
            k = 0
            while k < len(chs) and (chs[k][0].isdigit() or chs[k][0] in "*\u2020\u2021\u00a7\u00b6\u2217"):
                k += 1
            if 0 < k <= 3 and k < len(chs) and chs[0][1] < 0.85 * chs[k][1] and chs[k][1] < 0.97 * PF.body:
                notes[round(chs[k][1] * 2) / 2] += 1
    PF.note = notes.most_common(1)[0][0] if notes else round(0.85 * PF.body * 2) / 2
    global BODY_PT
    BODY_PT = PF.body
    # language: a handful of function words
    words = collections.Counter()
    for pno in sample[: 30]:
        words.update(w.lower() for w in re.findall(r"[A-Za-z\u00e0\u00e8\u00e9\u00ec\u00f2\u00f9]+", doc[pno].get_text()))
    score = {"it": sum(words[w] for w in ("il", "della", "che", "di", "per", "sono", "nel")),
             "en": sum(words[w] for w in ("the", "of", "and", "is", "which", "with", "that")),
             "de": sum(words[w] for w in ("der", "die", "und", "ist", "mit", "das")),
             "fr": sum(words[w] for w in ("le", "les", "des", "est", "une", "dans")),
             "es": sum(words[w] for w in ("el", "los", "las", "que", "una", "por"))}
    PF.lang = max(score, key=score.get) if any(score.values()) else "it"
    return PF


NOTE_OPEN, NOTE_CLOSE = "\ue000", "\ue001"   # sentinel around a note id in the text
IMG_OPEN, IMG_CLOSE = "\ue002", "\ue003"     # sentinel around an inline math image
NOT_NOTE_WORDS = {"cos", "sin", "tan", "cm", "mm", "m", "s", "km", "eV", "keV", "MeV", "GeV", "TeV",
                  "log", "ln", "exp", "kg", "g", "sec", "cot", "nm", "fm", "K"}


NOTE_SYMBOLS = "*†‡§¶∗"


def is_text_font(f):
    return not is_math_font(f) or font_family(f) in PF.text_fonts


def split_footnotes(lines, page_h):
    """Separate the footnote block at the bottom of a page.

    Returns (body_lines, notes, continuation) where notes is a list of
    (number, [lines]) and continuation the note lines that precede the first
    marker (the tail of a note started on the previous page)."""
    body_sz = [ln for ln in lines if abs(line_base(ln.chars)[0] - PF.body) < 0.3]
    floor = max((ln.bbox[3] for ln in body_sz), default=0)
    left = collections.Counter(round(ln.bbox[0]) for ln in lines).most_common(1)[0][0] if lines else 0
    cand = []
    for ln in lines:
        size, _ = line_base(ln.chars)
        if ln.bbox[1] < 0.55 * page_h or ln.bbox[1] < floor - 1 or not PF.note - 0.6 <= size <= PF.note + 0.35:
            continue
        cand.append(ln)
    cand.sort(key=lambda l: (l.bbox[1], l.bbox[0]))
    # a note starts with a small raised number (or *, †...) at the left margin, then note text
    def marker(ln):
        chs = [c for c in ln.chars if not c.c.isspace()]
        k = 0
        while k < len(chs) and k < 3 and (chs[k].c.isdigit() or chs[k].c in NOTE_SYMBOLS) \
                and chs[k].size < 0.87 * PF.note:
            k += 1
        if k and k < len(chs) and chs[0].bbox[0] < left + 0.6 * PF.body and chs[k].size > PF.note - 0.6:
            return "".join(c.c for c in chs[:k]), k
        return None
    starts = [ln for ln in cand if marker(ln)]
    if not starts and not cand:
        return lines, [], []
    first_y = starts[0].bbox[1] if starts else 1e9
    # continuation lines (tail of the previous page's note) are indented like note text,
    # captions instead start at the margin
    cont = [ln for ln in cand if ln.bbox[1] < first_y and left + 0.6 * PF.body < ln.bbox[0] < left + 1.4 * PF.body]
    if cont:
        cids = {id(l) for l in cand}
        floor2 = max((l.bbox[3] for l in lines if id(l) not in cids), default=0)
        cont = [ln for ln in cont if ln.bbox[1] >= floor2 - 1]
    notes = []
    for ln in cand:
        if ln.bbox[1] < first_y:
            continue
        m = marker(ln)
        if m:
            num, k = m
            # blank out the marker digits
            nonsp = [c for c in ln.chars if not c.c.isspace()][:k]
            ln.chars = [c for c in ln.chars if c not in nonsp]
            notes.append((num, [ln]))
        elif notes:
            notes[-1][1].append(ln)
    used = {id(l) for l in cont} | {id(l) for _, ls in notes for l in ls}
    # sub/superscripts of note lines (smaller size) that sit inside the note area
    if used:
        top = min(l.bbox[1] for l in lines if id(l) in used)
        for ln in lines:
            if id(ln) not in used and ln.bbox[1] >= top - 1 and line_base(ln.chars)[0] < PF.note - 0.6 and ln.bbox[1] > floor:
                owner = min((nl for _, nls in notes for nl in nls), default=None,
                            key=lambda nl: abs((nl.bbox[1] + nl.bbox[3]) / 2 - (ln.bbox[1] + ln.bbox[3]) / 2))
                if owner is not None:
                    for _, nls in notes:     # keep it as its own line: fractions stay stacked
                        if owner in nls:
                            nls.append(ln)
                    used.add(id(ln))
    body = [ln for ln in lines if id(ln) not in used]
    return body, notes, cont


def find_noteref(lines, num):
    """Locate the raised reference number `num` in running text; returns its chars."""
    target = str(num)
    best = []
    for row in group_rows(lines):
        if stacked(row):        # fractions and other two-level math: never a note mark
            continue
        chs = sorted((c for ln in row.lines for c in ln.chars if c.c), key=lambda c: c.bbox[0])
        main = [c for c in chs if not c.c.isspace()]
        if not main:
            continue
        size, base_y = line_base(main)
        if size < 0.95 * PF.body:
            continue
        sym = target in NOTE_SYMBOLS or not target.isdigit()
        k = 0
        while k < len(chs):
            c = chs[k]
            ok = (c.c in NOTE_SYMBOLS and c.oy < base_y + 0.5) if sym else c.c.isdigit()
            if not (ok and (c.size < 0.8 * size or sym) and (c.oy < base_y - 1 or sym and c.c != "*")
                    and not is_symbolic(c.font)):
                k += 1
                continue
            j = k
            while j < len(chs) and (chs[j].c in NOTE_SYMBOLS if sym else chs[j].c.isdigit()) \
                    and (chs[j].size < 0.8 * size or sym):
                j += 1
            run = chs[k:j]
            k = j
            if "".join(x.c for x in run) != target:
                continue
            before = [x for x in chs[:chs.index(run[0])] if not x.c.isspace()]
            prev = before[-1] if before else None
            word = ""
            if prev is not None and prev.c in ACCENTS and len(before) > 1:
                prev = before[-2]        # TeX accent drawn after its letter
            for x in reversed(before):
                if x.c in ACCENTS:
                    continue
                if not x.c.isalpha() or x.size < 0.8 * size:
                    break
                word = x.c + word
            nxt = next((x for x in chs[j:] if not x.c.isspace()), None)
            touching = nxt is not None and nxt.bbox[0] - run[-1].bbox[2] < 1.0 \
                and not any(x.c.isspace() for x in chs[j:chs.index(nxt)])
            if nxt is not None and (nxt.c.isdigit() or nxt.size < 0.8 * size or
                                    (touching and is_symbolic(nxt.font))):
                continue
            score = 0
            if prev is None:
                score = 1
            elif prev.c.isalpha() and is_text_font(prev.font) and prev.size >= 0.8 * size \
                    and word not in NOT_NOTE_WORDS:
                score = 3
            elif prev.c in ".,;:’'”»" and is_text_font(prev.font):
                score = 2
            elif prev.c == ")" and not any(is_symbolic(x.font) for x in before[-8:]):
                score = 1
            elif nxt is None or nxt.c in ".,;:)":
                score = 1   # after a math symbol, but followed by prose punctuation
            if score:
                best.append((score, row.bbox[1], run))
    if not best:
        return None
    best.sort(key=lambda b: (-b[0], b[1]))
    return best[0][2]


def group_rows(lines):
    """Lines that overlap vertically (fractions, eq numbers, split spans) form a row."""
    lines = sorted(lines, key=lambda l: (l.bbox[1], l.bbox[0]))
    rows = []

    def wordy(ln):
        return sum(1 for c in ln.chars if c.c.isalpha() and is_text_font(c.font)) >= 6

    def separate(r, ln):
        # two wide prose lines on different baselines are never one row, even when
        # a tall glyph (√, big brackets) makes their boxes overlap
        if PF.slides and wordy(ln):      # slide fonts have huge line boxes
            s1, y1 = line_base(ln.chars)
            for o in r.lines:
                if wordy(o):
                    s2, y2 = line_base(o.chars)
                    if abs(y1 - y2) > 0.6 * min(s1, s2):
                        return True
        wide = 0.45 * COL["w"]
        if ln.bbox[2] - ln.bbox[0] < wide:
            return False
        s1, y1 = line_base(ln.chars)
        for o in r.lines:
            if o.bbox[2] - o.bbox[0] >= wide:
                s2, y2 = line_base(o.chars)
                if abs(y1 - y2) > 0.8 * min(s1, s2):
                    return True
        return False

    for ln in lines:
        h = ln.bbox[3] - ln.bbox[1]
        target = None
        for r in rows[-4:]:
            ov = min(r.bbox[3], ln.bbox[3]) - max(r.bbox[1], ln.bbox[1])
            rh = min(h, r.bbox[3] - r.bbox[1])
            if ov > 0.45 * rh and not separate(r, ln):
                target = r
        if target:
            target.lines.append(ln)
            target.bbox = rect_union(target.bbox, ln.bbox)
        else:
            rows.append(Row([ln], ln.bbox))
    # merging can make rows overlap each other; merge again
    merged = []
    for r in sorted(rows, key=lambda r: r.bbox[1]):
        if merged:
            p = merged[-1]
            ov = min(p.bbox[3], r.bbox[3]) - max(p.bbox[1], r.bbox[1])
            if ov > 0.45 * min(p.bbox[3] - p.bbox[1], r.bbox[3] - r.bbox[1]) \
                    and not any(separate(p, ln) for ln in r.lines):
                p.lines += r.lines
                p.bbox = rect_union(p.bbox, r.bbox)
                continue
        merged.append(r)
    return merged


def row_stats(row):
    text = mathc = 0
    for ln in row.lines:
        for ch in ln.chars:
            if not ch.c.isalpha() and ch.c != "￼":
                continue
            if is_math_font(ch.font) or is_math_char(ch.c) or ch.c == "￼":
                mathc += 1
            else:
                text += 1
    return text, mathc


def stacked(row):
    ls = row.lines
    for i in range(len(ls)):
        for j in range(i + 1, len(ls)):
            a, b = ls[i].bbox, ls[j].bbox
            xov = min(a[2], b[2]) - max(a[0], b[0])
            cy = abs((a[1] + a[3]) / 2 - (b[1] + b[3]) / 2)
            if xov > 3 and cy > 3:
                return True
    return False


def is_bullet(t):
    t = t.lstrip()
    return bool(t) and (t[0] in "•▪◦‣∙●○■□➢➤►▶✓✔" or re.match(r"[-–—*]\s", t) is not None)


def classify_row(row, left, bars, right=None):
    if getattr(row, "system", False):
        return "display"
    text, mathc = row_stats(row)
    tot = text + mathc
    tr = text / tot if tot else 1
    has_cmex = any(BIGOP_RE.match(base_font(ch.font)) or ch.c == "￼"
                   for ln in row.lines for ch in ln.chars)
    right = right if right is not None else left + COL["w"]
    has_eqnum = any(EQNUM_RE.match(ln.text.strip()) and ln.bbox[0] > left + 0.6 * (right - left)
                    for ln in row.lines)
    # fraction bar: a thin rule with glyphs both above and below it
    has_frac = False
    chars = [c for ln in row.lines for c in ln.chars if not c.c.isspace() and c.c != "√"]
    for b in bars:
        if b[2] - b[0] < 3 or b[3] - b[1] > 1.5:
            continue
        under = [c for c in chars if b[0] - 1 <= (c.bbox[0] + c.bbox[2]) / 2 <= b[2] + 1]
        y = (b[1] + b[3]) / 2
        if any(c.oy < y + 0.5 for c in under) and any(c.oy - 0.6 * c.size > y - 0.8 for c in under):
            has_frac = True
            break
    indent = row.bbox[0] - left
    if has_eqnum:
        return "display"
    has_root = any(ch.c == "√" for ln in row.lines for ch in ln.chars)
    # systems with a big brace / stacked delimiters over several baselines
    big = sum(1 for ln in row.lines for ch in ln.chars if BIGOP_RE.match(base_font(ch.font)) and ch.c.strip())
    if big >= 2 and stacked(row) and len({round(line_base(ln.chars)[1]) for ln in row.lines}) >= 3:
        return "display"
    # inline roots stay text (√ + overline); prose lines with a fraction become images
    if has_frac or (tr < 0.6 and (has_cmex or has_root)):
        return "display"
    if indent > 1.2 * PF.body and tr < 0.35 and tot > 0:
        return "display"
    return "text"


# ---------------------------------------------------------------- inline html
def char_style(ch, base_size, base_y):
    f = ch.font
    it = bool(ITALIC_RE.search(base_font(f))) or bool(ch.flags & 2)
    bd = bool(BOLD_RE.search(base_font(f))) or bool(ch.flags & 16)
    vert = ""
    if ch.size < 0.85 * base_size:
        if ch.oy < base_y - 1.0:
            vert = "sup"
        elif ch.oy > base_y + 1.0:
            vert = "sub"
    return (it, bd, vert)


def line_base(chars):
    sizes = collections.Counter(round(ch.size, 1) for ch in chars if not ch.c.isspace())
    base_size = sizes.most_common(1)[0][0] if sizes else PF.body
    ys = collections.Counter(round(ch.oy, 1) for ch in chars if round(ch.size, 1) == base_size)
    base_y = ys.most_common(1)[0][0] if ys else chars[0].oy
    return base_size, base_y


def fix_text(s):
    """Compose TeX-style accents (`e -> è), expand ligatures, drop placeholders."""
    for k, v in LIGATURES.items():
        s = s.replace(k, v)
    out = []
    pending = None
    for c in s:
        if c in ACCENTS:
            if pending:
                out.append(pending)
            pending = c
            continue
        if pending:
            if c.isalpha() or c == "ı":
                out.append(("i" if c == "ı" else c) + ACCENTS[pending])
            else:
                out.append(pending)
                out.append(c)
            pending = None
            continue
        out.append(c)
    if pending:
        out.append(pending)
    s = "".join(out).replace("￼", "")
    return unicodedata.normalize("NFC", s)


def runs_to_html(runs):
    """runs: list of (text, (it, bd, vert)). Accents may straddle run boundaries."""
    # move a trailing accent onto the next run so fix_text can compose it
    runs = [list(r) for r in runs if r[0]]
    for i in range(len(runs) - 1):
        while runs[i][0] and runs[i][0][-1] in ACCENTS and runs[i + 1][0][:1].strip():
            runs[i + 1][0] = runs[i][0][-1] + runs[i + 1][0]
            runs[i][0] = runs[i][0][:-1]
    out = []
    for text, (it, bd, vert) in runs:
        t = html.escape(fix_text(text), quote=False)
        if not t:
            continue
        if it and t.strip():
            t = f"<i>{t}</i>"
        if bd and t.strip():
            t = f"<b>{t}</b>"
        if vert and t.strip():
            t = f"<{vert}>{t}</{vert}>"
        out.append(t)
    h = "".join(out)
    for tag in ("i", "b", "sup", "sub"):  # join adjacent identical tags
        h = h.replace(f"</{tag}><{tag}>", "")
    h = re.sub(IMG_OPEN + r"([^|]+)\|([^|]+)\|([^|]+)\|([^|]*)" + IMG_CLOSE,
               lambda m: (f'<img class="im" src="img/{m.group(1)}" alt="{m.group(4)}" '
                          f'style="height:{m.group(2)}em;vertical-align:{m.group(3)}em"/>'), h)
    return re.sub(NOTE_OPEN + r"(\d+)\|(\d+)" + NOTE_CLOSE,
                  lambda m: (f'<sup class="noteref"><a epub:type="noteref" href="#fn{m.group(1)}" '
                             f'id="fnr{m.group(1)}">{m.group(2)}</a></sup>'), h)


def line_runs(line):
    chars = line.chars
    base_size, base_y = line_base(chars)
    runs = []
    for ch in chars:
        st = char_style(ch, base_size, base_y)
        if NOTE_OPEN in ch.c or IMG_OPEN in ch.c:
            st = (False, False, "")
        c = ch.c + ("̅" if ch.overline else "")
        if ch.c.isspace():
            st = (False, False, "") if not runs else runs[-1][1]
        if runs and runs[-1][1] == st:
            runs[-1][0] += c
        else:
            runs.append([c, st])
    return runs


def row_runs(row):
    if len(row.lines) == 1:
        return line_runs(row.lines[0])
    chars = sorted((c for ln in row.lines for c in ln.chars), key=lambda c: c.bbox[0])
    # TeX accents sit over their letter and may sort after it: put them first again
    for k in range(len(chars) - 1):
        a, b = chars[k], chars[k + 1]
        if b.c in ACCENTS and a.c.isalpha() and a.bbox[0] <= (b.bbox[0] + b.bbox[2]) / 2 <= a.bbox[2]:
            chars[k], chars[k + 1] = b, a
    seq = []
    for c in chars:
        if seq and not c.c.isspace() and not seq[-1].c.isspace() and c.bbox[0] - seq[-1].bbox[2] > 0.2 * c.size:
            seq.append(Char(" ", c.bbox, c.size, c.font, c.flags, c.oy))
        seq.append(c)
    return line_runs(Line(seq, row.bbox))


def plain(row):
    t = re.sub(IMG_OPEN + r"[^|]*\|[^|]*\|[^|]*\|([^" + IMG_CLOSE + r"]*)" + IMG_CLOSE, r"\1", row.text)
    t = re.sub(NOTE_OPEN + r"[^" + NOTE_CLOSE + r"]*" + NOTE_CLOSE, "", t)
    return fix_text(t)


# ---------------------------------------------------------------- graphics
def is_label_font(f):
    """Font of figure lettering rather than of the running text or of the formulas."""
    return font_family(f) not in PF.text_fonts and (not is_math_font(f) or font_family(f) == "Symbol")


def slide_prose(text, size):
    """On slides, sentences are running text whatever their font (text boxes in Arial...)."""
    return PF.slides and len(text.strip()) >= 25 and size >= 0.7 * PF.body and " " in text.strip()


def graphic_rects(page):
    """Return (regions, bars): big figure/table rectangles and thin rules."""
    pr = page.rect
    top, bot = PF.header_y, min(PF.footer_y, pr.height)

    def keep(r):
        w, h = r[2] - r[0], r[3] - r[1]
        if w > 0.85 * pr.width and h > 0.85 * pr.height:
            return False      # page background / frame
        return r[3] > top and r[1] < bot
    rects = [tuple(fitz.Rect(i["bbox"]) & pr) for i in page.get_image_info()]
    rects = [r for r in rects if keep(r) and r[2] > r[0] and r[3] > r[1]]
    comps = merge_rects(rects, 1.5)
    # vector drawings are only used for figures here: group them generously,
    # together with label text set in non-text fonts (Arial, Helvetica, Type3...)
    draws = [tuple(d["rect"]) for d in page.get_drawings()
             if keep(tuple(d["rect"])) and not (d.get("fill") == (1.0, 1.0, 1.0) and not d.get("color"))]
    # isolated thin rules (fraction bars, underlines) are not figures
    thin = [d for d in draws if min(d[2] - d[0], d[3] - d[1]) <= 3]
    lone = [d for d in thin if not any(o is not d and touches(d, o, 6) for o in draws)]
    draws = [d for d in draws if d not in lone]
    real = list(draws)
    if draws:
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                txt = "".join(s["text"] for s in l["spans"]).strip()
                if not txt or not keep(l["bbox"]):
                    continue
                if slide_prose(txt, max(s["size"] for s in l["spans"])):
                    continue
                if PF.slides and all(is_math_font(s["font"]) for s in l["spans"] if s["text"].strip()):
                    continue      # formula pieces (and their fraction bars) are equations, not figures
                if all(is_label_font(s["font"]) for s in l["spans"] if s["text"].strip()) \
                        or l["dir"][0] < 0.9 or (PF.slides and len(txt) <= 10):
                    draws.append(tuple(l["bbox"]))
        for r in merge_rects(draws, 12 if not PF.slides else 6):
            if PF.slides and not any(area_overlap(d, r) > 0 or touches(d, r, 0) and d[0] >= r[0] - 0.1 and d[2] <= r[2] + 0.1
                       for d in real):
                continue      # only labels, no drawing: that is text
            if r[2] - r[0] >= MIN_FIG_W and r[3] - r[1] >= 15:
                comps.append(r)
    regions, bars = [], list(lone)
    for r in comps:
        w, h = r[2] - r[0], r[3] - r[1]
        if w >= MIN_FIG_W and h >= MIN_FIG_H:
            regions.append(r)
        else:
            bars.append(r)
    return regions, bars


def table_regions(page, bars, rows):
    """Tables drawn only with horizontal rules (no vertical lines)."""
    if not any(CAPTION_RE.match(plain(r)) and TABLE_CAP_RE.match(plain(r)) for r in rows):
        return []
    hr = sorted([b for b in bars if b[2] - b[0] > 100 and b[3] - b[1] < 2], key=lambda b: b[1])
    groups = []
    for b in hr:
        if groups:
            g = groups[-1]
            last = g[-1]
            if abs(b[0] - last[0]) < 6 and abs(b[2] - last[2]) < 6 and b[1] - last[3] < 250:
                g.append(b)
                continue
        groups.append([b])
    out = []
    for g in groups:
        if len(g) >= 2:
            r = g[0]
            for b in g[1:]:
                r = rect_union(r, b)
            out.append(r)
    return out


def absorb_labels(region, rows, left, right):
    """Grow a figure region over the label text that sits on/around it."""
    changed = True
    while changed:
        changed = False
        for r in rows:
            if r.kind == "used":
                continue
            t = plain(r)
            if CAPTION_RE.match(t):
                continue
            label = all(is_label_font(c.font) for ln in r.lines for c in ln.chars if c.c.strip()) \
                or (PF.slides and len(t) <= 10)
            if slide_prose(t, max(c.size for ln in r.lines for c in ln.chars)):
                continue
            m = 25 if label else 6
            ex = (region[0] - m, region[1] - m, region[2] + m, region[3] + m)
            if not label:
                ex = (region[0] - 6, region[1] - 6, region[2] + 6, region[3] + 6)
            if not touches(r.bbox, ex, 0):
                continue
            inside = area_overlap(r.bbox, region) / max(1e-3, (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1]))
            wide_body = (r.bbox[2] - r.bbox[0]) > 0.75 * (right - left) and inside < 0.5
            if wide_body:
                continue
            region = rect_union(region, r.bbox)
            r.kind = "used"
            changed = True
    return region


def slide_regions(page, figs):
    """Slides: every text box and every figure is a region, read top to bottom, left to right
    when side by side (text boxes next to each other are never mixed line by line)."""
    import functools
    fboxes = merge_rects([tuple(f) for f in figs], -1)
    boxes = list(fboxes)
    tb, mb = [], []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0 or not "".join(s["text"] for l in b["lines"] for s in l["spans"]).strip():
            continue
        r = tuple(b["bbox"])
        n = sum(len(s["text"].strip()) for l in b["lines"] for s in l["spans"])
        nm = sum(len(s["text"].strip()) for l in b["lines"] for s in l["spans"] if is_math_font(s["font"]))
        if nm >= 0.6 * max(n, 1):
            mb.append(r)      # PowerPoint splits one equation into many small text boxes
            continue
        a = max(1e-3, (r[2] - r[0]) * (r[3] - r[1]))
        if any(area_overlap(r, f) > 0.6 * a for f in boxes):
            continue
        tb.append(r)
    boxes += merge_rects(tb, 1)
    mboxes = []
    for m in merge_rects(mb, 0.7 * PF.body):
        m = (m[0] - 3, m[1] - 3, m[2] + 3, m[3] + 3)      # fraction bars sit on the edge
        a = max(1e-3, (m[2] - m[0]) * (m[3] - m[1]))
        if not any(area_overlap(m, f) > 0.6 * a for f in fboxes):
            mboxes.append(m)
    # vector pieces of a formula (drawn brackets, overlines) belong to the formula
    for f in list(fboxes):
        fa = max(1e-3, (f[2] - f[0]) * (f[3] - f[1]))
        for k, m in enumerate(mboxes):
            ma = (m[2] - m[0]) * (m[3] - m[1])
            if area_overlap(f, m) > 0.5 * fa or (touches(f, m, 0.4 * PF.body) and fa < 1.5 * ma
                                                 and f[1] > m[1] - 0.5 * PF.body and f[3] < m[3] + 0.5 * PF.body):
                mboxes[k] = rect_union(m, f)
                fboxes.remove(f)
                boxes.remove(f)
                break
    boxes += mboxes

    def order(a, b):
        ov = min(a[3], b[3]) - max(a[1], b[1])
        if ov > 0.5 * min(a[3] - a[1], b[3] - b[1]):
            return -1 if a[0] < b[0] else 1
        return -1 if a[1] < b[1] else 1
    boxes.sort(key=functools.cmp_to_key(order))
    return [(b, "fig" if b in fboxes else "math" if b in mboxes else "box", max(b[2] - b[0], 1)) for b in boxes]


def column_regions(page, lines, figs):
    """Split the page into reading-order regions: [(rect, gutter_x or None, text_width)].

    Two-column pages become bands: full-width elements (title, abstract, wide figures,
    footnotes across the page) are bands of their own, the rest is read left column
    first, then right column."""
    W, H = page.rect.width, page.rect.height
    xs0 = sorted(l.bbox[0] for l in lines) or [0]
    xs1 = sorted(l.bbox[2] for l in lines) or [W]
    tl_, tr_ = xs0[len(xs0) // 20], xs1[-1 - len(xs1) // 20]
    full = [((0, 0, W, H), None, max(tr_ - tl_, 1))]
    if PF.slides:
        return [(b, kind, full[0][2]) for b, kind, _ in slide_regions(page, figs)] + full
    tl = [l for l in lines if l.bbox[2] - l.bbox[0] > 0.12 * W and len(l.text.strip()) > 8]
    if len(tl) < 8:
        return full
    best = None
    for g in range(int(0.3 * W), int(0.7 * W) + 1, 2):
        cross = sum(1 for l in tl if l.bbox[0] < g - 2 and l.bbox[2] > g + 2)
        if best is None or cross < best[0]:
            best = (cross, g)
    cross, g = best
    left = [l for l in tl if l.bbox[2] <= g + 2]
    right = [l for l in tl if l.bbox[0] >= g - 2]
    n = len(tl)
    if cross > 0.15 * n or min(len(left), len(right)) < max(4, 0.2 * n):
        return full
    # both columns must really be columns: lines of similar width side by side
    lw = sorted(l.bbox[2] - l.bbox[0] for l in left)[len(left) // 2]
    rw = sorted(l.bbox[2] - l.bbox[0] for l in right)[len(right) // 2]
    if lw < 0.25 * W or rw < 0.25 * W:
        return full
    gl = sorted(l.bbox[2] for l in left)[int(0.9 * (len(left) - 1))]
    gr = sorted(l.bbox[0] for l in right)[int(0.1 * (len(right) - 1))]
    g = (gl + gr) / 2 if gl < gr else g
    colw = max(gl - tl_, tr_ - gr, 1)
    # full-width bands: elements across the gutter with no column text beside them
    side = [l for l in lines if not (l.bbox[0] < g - 3 and l.bbox[2] > g + 3)]
    spans = []
    for b in [l.bbox for l in lines if l.bbox[0] < g - 3 and l.bbox[2] > g + 3] + \
             [f for f in figs if f[0] < g - 3 and f[2] > g + 3]:
        if not any(min(b[3], o.bbox[3]) - max(b[1], o.bbox[1]) > 1 for o in side):
            spans.append([b[1], b[3]])
    spans.sort()
    bands = []
    for y0, y1 in spans:
        if bands and y0 <= bands[-1][1] + 0.6 * PF.body:
            bands[-1][1] = max(bands[-1][1], y1)
        else:
            bands.append([y0, y1])
    out, cur = [], 0
    for y0, y1 in bands + [[H, H]]:
        y0 -= 0.5
        if y0 > cur:
            out.append(((0, cur, g, y0), g, colw))
            out.append(((g, cur, W, y0), g, colw))
        if y1 > y0 + 0.5:
            out.append(((0, y0, W, y1 + 0.5), None, max(tr_ - tl_, 1)))
        cur = y1 + 0.5
    return out


# ---------------------------------------------------------------- main page pass
class Converter:
    def __init__(self, pdf, images=None, prefix=""):
        self.doc = fitz.open(pdf)
        self.images = images if images is not None else {}   # name -> bytes (shared between PDFs)
        self.prefix = prefix
        self.prose_rects = {}       # pno -> char boxes of running text (removed from slide figures)
        self.clean = {}             # (pno, variant) -> cleaned copy of the page for rendering
        self.rendered = {}          # pno -> rects turned into pictures
        self.report = {"ctrl": collections.Counter(), "eq_imgs": 0, "fig_imgs": 0,
                       "missing_headings": [], "pages": 0}
        self.toc = self.doc.get_toc()
        self.note_seq = 0
        self.last_note = None       # open note block, for notes continuing on the next page
        self.page_notes = []        # note blocks produced by the last page_items call
        self.report["notes"] = []

    def render_page(self, pno, kind):
        """The page to render pictures from: banned fonts (Comic Sans) are redrawn in a sans
        serif, and on slides the running text overlapping a figure is removed."""
        page = self.doc[pno]
        prose = self.prose_rects.get(pno, []) if (PF.slides and kind == "fig") else []
        key = (pno, bool(prose))
        if key in self.clean:
            return self.clean[key][0]
        spans = [sp for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])
                 for sp in l["spans"] if sp["text"].strip()]
        banned = [sp for sp in spans if any(x in sp["font"] for x in BANNED_FONTS)]
        if not banned and not prose:
            self.clean[key] = (page, None)
            return page
        tmp = fitz.open()
        tmp.insert_pdf(self.doc, from_page=pno, to_page=pno)
        cp = tmp[0]

        def band(b, oy, size):      # a thin strip through the glyphs: does not touch other lines
            return fitz.Rect(b[0] + 0.3, oy - 0.55 * size, b[2] - 0.3, oy - 0.15 * size)
        # one strip per stretch of text on a baseline (one redaction per char is very slow)
        gone = []
        for b, oy, sz in sorted(prose, key=lambda p: (round(p[1]), p[0][0])):
            r = band(b, oy, sz)
            if gone and abs(gone[-1][1] - oy) < 0.5 and r.x0 - gone[-1][0].x1 < 2 * sz:
                gone[-1][0] |= r
            else:
                gone.append([r, oy])
        gone = [g for g, _ in gone]
        for r in gone:
            cp.add_redact_annot(r, fill=False)
        relabel = []
        for sp in banned:
            r = band(sp["bbox"], sp["origin"][1], sp["size"])
            sb, soy = sp["bbox"], sp["origin"][1]
            if any(abs(oy - soy) < 1.5 and b[0] >= sb[0] - 1 and b[2] <= sb[2] + 1 for b, oy, _ in prose):
                continue        # running text: removed from the picture, not redrawn
            cp.add_redact_annot(r, fill=False)
            relabel.append(sp)
        cp.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        for sp in relabel:
            bold = bool(BOLD_RE.search(sp["font"]))
            name = "F1b" if bold else "F1"
            ff = SANS_BOLD if bold else SANS
            try:
                if ff:
                    cp.insert_font(fontname=name, fontfile=ff)
                else:
                    name = "hebo" if bold else "helv"
            except Exception:
                name = "hebo" if bold else "helv"
            c = sp["color"]
            cp.insert_text(sp["origin"], sp["text"], fontname=name, fontsize=sp["size"] * 0.92,
                           color=((c >> 16) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255))
        self.clean[key] = (cp, tmp)
        return cp

    def render(self, pno, rect, kind):
        page = self.render_page(pno, kind)
        pad = 2 if kind in ("eq", "im") else 3
        hpad = 0.3 if kind == "im" else pad
        r = fitz.Rect(rect[0] - hpad, rect[1] - pad, rect[2] + hpad, rect[3] + pad) & page.rect
        self.rendered.setdefault(pno, []).append(r)
        if kind == "im":      # inline math: sharper, stored as an equation picture
            pix = page.get_pixmap(matrix=fitz.Matrix(4, 4), clip=r, colorspace=fitz.csGRAY)
            data, ext, kind = pix.tobytes("png"), "png", "eq"
            self.report["eq_imgs"] += 1
        elif kind == "eq":
            pix = page.get_pixmap(matrix=fitz.Matrix(EQ_ZOOM, EQ_ZOOM), clip=r, colorspace=fitz.csGRAY)
            data, ext = pix.tobytes("png"), "png"
            self.report["eq_imgs"] += 1
        else:
            pix = page.get_pixmap(matrix=fitz.Matrix(FIG_ZOOM, FIG_ZOOM), clip=r)
            data, ext = pix.tobytes("png"), "png"
            if len(data) > 120_000:
                data, ext = pix.tobytes("jpeg", jpg_quality=82), "jpg"
            self.report["fig_imgs"] += 1
        name = f"{self.prefix}p{pno + 1:03d}_{kind}{len(self.images) + 1:04d}.{ext}"
        self.images[name] = data
        return name, r.width, r.height

    def img_item(self, pno, rect, kind, alt=""):
        name, w, h = self.render(pno, rect, kind)
        alt = html.escape(alt[:300], quote=True)
        if kind == "eq":
            style = f"width:{w / BODY_PT:.2f}em"
            h_ = f'<div class="eq"><img src="img/{name}" alt="{alt}" style="{style}"/></div>'
        else:
            pct = min(100, 100 * w / max(COL["w"], 1))
            h_ = f'<div class="fig"><img src="img/{name}" alt="{alt}" style="width:{pct:.0f}%"/></div>'
        return Item("img", rect[1], rect[3], html=h_, page=pno)

    def page_items(self, pno):
        """Items of a page in reading order (columns and full-width bands)."""
        page = self.doc[pno]
        lines = page_lines(page, self.report, pno)
        self.page_notes = []
        self.prose_rects[pno] = []
        g_regions, g_bars = graphic_rects(page)
        if not lines and not g_regions:
            return []
        cols = column_regions(page, lines, g_regions)
        if any(isinstance(c[1], (int, float)) for c in cols):
            self.report["two_column_pages"] = self.report.get("two_column_pages", 0) + 1
        items = []
        taken = set()
        if PF.slides:
            # text belongs to its text box first, then to a figure, then to the page
            def owner(b, kinds):
                cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                for kind in kinds:
                    for k, (rect, side, _) in enumerate(cols):
                        if side == kind and rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3]:
                            return k
                return len(cols) - 1
            maths = [c[0] for c in cols if c[1] == "math"]
            for g in list(g_regions):     # drawn brackets/overlines of a formula: fraction-bar like
                ga = max(1e-3, (g[2] - g[0]) * (g[3] - g[1]))
                if any(area_overlap(g, m) > 0.5 * ga for m in maths):      # (math boxes include them)
                    g_regions.remove(g)
                    g_bars.append(g)
            own = {id(l): owner(l.bbox, ("box", "math", "fig")) for l in lines}
            own.update({id(g): owner(g, ("fig", "math", "box")) for g in g_regions + g_bars})
            # running text is never part of a picture
            for l in lines:
                if cols[own[id(l)]][1] in ("box", "math") or slide_prose(l.text, line_base(l.chars)[0]):
                    self.prose_rects[pno] += [(c.bbox, c.oy, c.size) for c in l.chars if c.c.strip()]
        for k, (rect, side, width) in enumerate(cols):
            def inside(b, line=None):
                cy = (b[1] + b[3]) / 2
                if not rect[1] <= cy < rect[3]:
                    return False
                if PF.slides:
                    return own.get(id(b)) == k
                x = b[0] if line else (b[0] + b[2]) / 2
                return side is None or (x < side) == (rect[0] < side)
            sub = [l for l in lines if id(l) not in taken and (own.get(id(l)) == k if PF.slides else inside(l.bbox, True))]
            regs = [r for r in g_regions if id(r) not in taken and (own.get(id(r)) == k if PF.slides else inside(r))]
            bars = [b for b in g_bars if id(b) not in taken and (own.get(id(b)) == k if PF.slides else inside(b))]
            taken |= {id(x) for x in sub + regs + bars}
            if not sub and not regs:
                continue
            COL["w"] = width
            its = self.region_items(pno, page, sub, regs, bars, rect if side == "math" else None)
            for it in its:
                it.region = k
            items += its
        return items

    def region_items(self, pno, page, lines, regions, bars, math_rect=None):
        lines, notes, cont = split_footnotes(lines, page.rect.height)
        if cont and self.last_note is not None and self.last_note["page"] == pno \
                and not re.search(r"[.!?;)]\s*$", "".join(c.c for c in self.last_note["rows"][-1][1].lines[-1].chars)) \
                and min(l.bbox[1] for l in cont) > 0.8 * page.rect.height:
            self.last_note["rows"] += [(pno, r) for r in group_rows(cont)]
        elif cont:
            lines += cont         # not a note after all: give the lines back to the page
        for num, nls in notes:
            self.note_seq += 1
            nid = self.note_seq
            blk = {"kind": "note", "id": nid, "num": num, "rows": [(pno, r) for r in group_rows(nls)],
                   "page": pno + 1}
            ref = find_noteref(lines, num)
            if ref is None and pno > 0:
                ref_where = "assente"
            else:
                ref_where = "trovato"
            if ref is not None:
                ref[0].c = f"{NOTE_OPEN}{nid}|{num}{NOTE_CLOSE}"
                for x in ref[1:]:
                    x.c = ""
            else:
                # no visible reference: attach the note to the last body line of the page
                tail = max(lines, key=lambda l: l.bbox[3])
                tail.chars.append(Char(f"{NOTE_OPEN}{nid}|{num}{NOTE_CLOSE}", tail.chars[-1].bbox,
                                       tail.chars[-1].size, tail.chars[-1].font, 0, tail.chars[-1].oy))
            self.report["notes"].append((pno + 1, num, ref_where))
            self.page_notes.append(blk)
            self.last_note = blk
        if not lines:          # a picture without text
            items = []
            for reg in merge_rects(regions, 4):
                it = self.img_item(pno, reg, "fig")
                it.left, it.right = reg[0], reg[2]
                items.append(it)
            return items
        xs = collections.Counter(round(l.bbox[0]) for l in lines)
        left = xs.most_common(1)[0][0]
        right = max(collections.Counter(round(l.bbox[2]) for l in lines).most_common(3))[0]
        rows = group_rows(lines)
        regions += table_regions(page, bars, rows)
        regions = merge_rects(regions, 4)

        items = []
        # figures/tables
        final_regions = []
        for reg in regions:
            for r in rows:
                if area_overlap(r.bbox, reg) > 0.5 * (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1]) \
                        and not slide_prose(plain(r), self.row_size(r)):
                    r.kind = "used"
            reg = absorb_labels(reg, rows, left, right)
            final_regions.append(reg)
        final_regions = merge_rects(final_regions, 2)
        merged = True
        while merged:
            merged = False
            for a in range(len(final_regions)):
                for b in range(a + 1, len(final_regions)):
                    A, B = final_regions[a], final_regions[b]
                    vov = min(A[3], B[3]) - max(A[1], B[1])
                    hgap = max(A[0], B[0]) - min(A[2], B[2])
                    if vov > 0.4 * min(A[3] - A[1], B[3] - B[1]) and hgap < 100:
                        final_regions[a] = rect_union(A, B)
                        del final_regions[b]
                        merged = True
                        break
                if merged:
                    break
        for reg in final_regions:
            for r in rows:
                if r.kind != "used" and area_overlap(r.bbox, reg) > 0.5 * (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1]) \
                        and not slide_prose(plain(r), self.row_size(r)):
                    r.kind = "used"

        bars = [b for b in bars if not any(touches(b, reg, 1) for reg in final_regions)]
        rows = [r for r in rows if r.kind != "used"]
        for r in rows:
            r.kind = classify_row(r, left, bars, right)
        # figures are rendered without the running text that overlaps them (slides)
        for reg in final_regions:
            for r in rows:
                for ln in r.lines:
                    for c in ln.chars:
                        if c.c.strip() and touches(c.bbox, reg, 0):
                            self.prose_rects[pno].append((c.bbox, c.oy, c.size))
        for reg in final_regions:
            items.append(self.img_item(pno, reg, "fig"))

        # overlines (antiparticles): short thin rules just above characters of text rows
        for b in bars:
            if b[3] - b[1] > 1.2:
                continue
            for r in rows:
                if r.kind != "text" or not (r.bbox[1] - 3 <= b[1] <= r.bbox[3]):
                    continue
                root = any(c.c == "√" and abs(c.bbox[2] - b[0]) < 3 for ln in r.lines for c in ln.chars)
                if b[2] - b[0] > 25 and not root:
                    continue
                hit = [ch for ln in r.lines for ch in ln.chars
                       if b[0] - 0.5 <= (ch.bbox[0] + ch.bbox[2]) / 2 <= b[2] + 0.5
                       and b[3] < ch.oy - 0.45 * ch.size and b[1] > ch.oy - 1.4 * ch.size
                       and not ch.c.isspace()]
                if hit and (root or max(c.bbox[2] for c in hit) - min(c.bbox[0] for c in hit) >= 0.6 * (b[2] - b[0])):
                    for ch in hit:
                        ch.overline = True

        # display equations: merge consecutive display rows
        groups = []
        for r in rows:
            if r.kind != "display":
                continue
            if groups and r.bbox[1] - groups[-1][-1].bbox[3] < max(5, 0.45 * PF.body) \
                    and (not PF.slides or min(r.bbox[2], max(x.bbox[2] for x in groups[-1]))
                         > max(r.bbox[0], min(x.bbox[0] for x in groups[-1]))):
                groups[-1].append(r)
            else:
                groups.append([r])
        for g in groups:
            reg = g[0].bbox
            for r in g[1:]:
                reg = rect_union(reg, r.bbox)
            # pull in numerators/denominators that sit just past a fraction bar
            changed = True
            while changed:
                changed = False
                near = [b for b in bars if touches(b, reg, 1.5)]
                for b in near:
                    reg = rect_union(reg, b)
                for r in rows:
                    if r.kind == "text" and any(touches(r.bbox, b, 3) for b in near) \
                            and (r.bbox[2] - r.bbox[0]) < 0.6 * (right - left):
                        r.kind = "eqpart"
                        g.append(r)
                        reg = rect_union(reg, r.bbox)
                        changed = True
            if math_rect is not None:     # a whole formula box (slides): keep drawn brackets, overlines
                reg = rect_union(reg, math_rect)
            items.append(self.img_item(pno, reg, "eq", alt=" ".join(plain(r) for r in g)))

        emitted = set()
        for r in rows:
            if r.kind == "text":
                r = self.inline_math(r, pno)
                emitted |= {id(c) for ln in r.lines for c in ln.chars}
                items.append(Item("para", r.bbox[1], r.bbox[3], rows=[r], page=pno))
        # formula check: every math glyph must be in the text or inside a picture
        done = self.rendered.get(pno, [])
        for ln in lines:
            for c in ln.chars:
                if not c.c.strip() or not (is_math_font(c.font) or is_math_char(c.c) or c.c == PLACEHOLDER):
                    continue
                self.report["math_glyphs"] = self.report.get("math_glyphs", 0) + 1
                cx, cy = (c.bbox[0] + c.bbox[2]) / 2, (c.bbox[1] + c.bbox[3]) / 2
                if id(c) in emitted or any(r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1 for r in done):
                    continue
                self.report["math_lost"] = self.report.get("math_lost", 0) + 1
                lp = self.report.setdefault("math_lost_pages", {})
                lp[pno + 1] = lp.get(pno + 1, 0) + 1
                self.report.setdefault("math_lost_chars", []).append(
                    (pno + 1, c.c, base_font(c.font), [round(v) for v in c.bbox]))
        items.sort(key=lambda it: it.y0)
        for it in items:
            it.left, it.right = left, right
        return items

    # ------------------------------------------------------------ inline math
    def inline_math(self, row, pno):
        """Replace stacked math (sub+sup, arrows over symbols, √...) and unmapped glyphs
        inside a text row with small inline images; the rest of the row stays text."""
        # stray big delimiters (CMEX) of a nearby display formula are not part of the text row
        def big_delim(ln):
            ch = [c for c in ln.chars if c.c.strip()]
            return ch and all(BIGOP_RE.match(base_font(c.font)) for c in ch)
        if len(row.lines) > 1 and any(big_delim(ln) for ln in row.lines):
            rest = [ln for ln in row.lines if not big_delim(ln)]
            if rest:
                row = Row(rest, row.bbox, row.kind)
        allc = [(k, c) for k, ln in enumerate(row.lines) for c in ln.chars if c.c.strip()]
        if not allc:
            return row
        size, base_y = line_base([c for _, c in allc])
        trouble = [c for _, c in allc if c.c == PLACEHOLDER]
        # true stacking: glyphs of different lines on top of each other (sub+sup, limits, arrows)
        SYM = "√→←−⇒⇐⇔↔∼˜^¯˙¨∑∏∫∮⊗⊕"

        def mathy(c):
            return (is_math_font(c.font) or is_math_char(c.c) or c.c.isdigit() or c.c in "+−-=()[]{},/|<>·∗*'′" + SYM
                    or c.size < 0.85 * size or c.c == PLACEHOLDER)

        def textish(c):      # an ordinary full-size letter of the running text
            return c.c.isalpha() and is_text_font(c.font) and not is_math_char(c.c) and c.size >= 0.85 * size

        if len(row.lines) > 1:
            for k, c in allc:
                if NOTE_OPEN in c.c or textish(c) or not mathy(c):
                    continue
                cw = c.bbox[2] - c.bbox[0]
                cy = (c.bbox[1] + c.bbox[3]) / 2
                for k2, d in allc:
                    if k2 == k or NOTE_OPEN in d.c or textish(d):
                        continue
                    if not (c.size < 0.85 * size or d.size < 0.85 * size or c.c in SYM or d.c in SYM
                            or is_math_font(c.font) and is_math_font(d.font)):
                        continue
                    ov = min(c.bbox[2], d.bbox[2]) - max(c.bbox[0], d.bbox[0])
                    if ov > 0.4 * min(cw, d.bbox[2] - d.bbox[0]) and abs(cy - (d.bbox[1] + d.bbox[3]) / 2) > 2.5:
                        trouble.append(c)
                        break
        # glyphs that only poke into this row from the line below/above (tall √, big
        # operators of a display formula) are not this row's math
        def in_band(c):
            cy = (c.bbox[1] + c.bbox[3]) / 2
            if BIGOP_RE.match(base_font(c.font)):     # tall delimiters: any overlap with the line
                return c.bbox[3] > base_y - 0.8 * size and c.bbox[1] < base_y
            return base_y - 1.25 * size <= cy <= base_y + 0.55 * size
        trouble = [c for c in trouble if in_band(c)]
        if not trouble:
            return row
        # intervals around the trouble chars, grown over the adjacent math in the main line
        ivs = sorted([c.bbox[0], c.bbox[2]] for c in trouble)
        merged = []
        for a, b in ivs:
            if merged and a <= merged[-1][1] + 1.5:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        mchars = sorted((c for _, c in allc), key=lambda c: c.bbox[0])

        for iv in merged:
            for direction in (-1, 1):
                grown = True
                while grown:
                    grown = False
                    for c in mchars:
                        cx0, cx1 = c.bbox[0], c.bbox[2]
                        near = (direction < 0 and iv[0] - 1.8 <= cx1 <= iv[0] + 0.3 and cx0 < iv[0]) or \
                               (direction > 0 and iv[1] - 0.3 <= cx0 <= iv[1] + 1.8 and cx1 > iv[1])
                        if near and c.c.strip() and mathy(c) and not textish(c) and NOTE_OPEN not in c.c:
                            iv[0], iv[1] = min(iv[0], cx0), max(iv[1], cx1)
                            grown = True
        # merge again after growing
        merged.sort()
        ivs, merged = merged, []
        for a, b in ivs:
            if merged and a <= merged[-1][1] + 1.5:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        y0, y1 = row.bbox[1], row.bbox[3]
        keep = []
        for c in sorted((c for ln in row.lines for c in ln.chars), key=lambda c: c.bbox[0]):
            cx = (c.bbox[0] + c.bbox[2]) / 2
            if not any(a - 0.2 <= cx <= b + 0.2 for a, b in merged) or NOTE_OPEN in c.c:
                keep.append(c)
        for a, b in merged:
            rect = (a, y0, b, y1)
            name, w, h = self.render(pno, rect, "im")
            self.report["inline_math"] = self.report.get("inline_math", 0) + 1
            # the picture is padded by 2pt in render(); express it in em of the body size
            pad = 2
            hem = (y1 - y0 + 2 * pad) / PF.body
            valign = -((y1 + pad) - base_y) / PF.body
            alt = re.sub(r'["|<>&\ue000-\ue003]', "", fix_text("".join(c.c for c in sorted(
                (c for ln in row.lines for c in ln.chars if a - 0.2 <= (c.bbox[0] + c.bbox[2]) / 2 <= b + 0.2),
                key=lambda c: c.bbox[0]))))
            keep.append(Char(f"{IMG_OPEN}{name}|{hem:.2f}|{valign:.2f}|{alt}{IMG_CLOSE}",
                             (a, base_y - size, b, base_y), size, "CMR10", 0, base_y))
        keep.sort(key=lambda c: c.bbox[0])
        # spaces around an image: keep the original spacing of the line
        out = []
        for c in keep:
            if out and c.c.isspace() and out[-1].c.isspace():
                continue
            if out and not c.c.isspace() and not out[-1].c.isspace() and c.bbox[0] - out[-1].bbox[2] > 0.2 * size:
                out.append(Char(" ", c.bbox, size, "CMR10", 0, base_y))
            out.append(c)
        ln = Line(out, row.bbox)
        return Row([ln], row.bbox, "text")

    # ------------------------------------------------------------ headings
    @staticmethod
    def norm(s):
        s = fix_text(s.replace("\r", ""))
        s = unicodedata.normalize("NFKD", s)
        return re.sub(r"[^a-z0-9]", "", s.lower())

    def apply_headings(self, items, pno, toc_here):
        """Turn TOC entries into headings, matched against bold rows on the page."""
        for level, title in toc_here:
            # some bookmarks end in mis-encoded garbage: keep the readable prefix
            title = re.split(r"[\ud800-\udfff\u3000-\u9fff\uac00-\ud7a3]", title)[0].strip()
            target = self.norm(title)
            best = None
            paras = [i for i in items if i.kind == "para"]
            for k, it in enumerate(paras):
                if not self.is_heading_like(it.rows[0]):
                    continue
                # chapter titles span up to three rows (number, title, continuation)
                acc = ""
                for j in range(k, min(k + 3, len(paras))):
                    if not self.is_heading_like(paras[j].rows[0]):
                        break
                    acc += self.norm(plain(paras[j].rows[0]))
                    ratio = difflib.SequenceMatcher(None, acc, target).ratio()
                    if len(target) >= 10 and acc.startswith(target):
                        ratio = max(ratio, 0.9)
                    if ratio > 0.82 and (best is None or ratio > best[0]):
                        best = (ratio, k, j)
            if best:
                _, k, j = best
                rows = [paras[x].rows[0] for x in range(k, j + 1)]
                text = " ".join(plain(r) for r in rows)
                if level == 1:
                    text = re.sub(r"^([0-9]+|[A-Z])\s+", r"\1 ", text)
                for x in range(k + 1, j + 1):
                    paras[x].kind = "drop"
                h = paras[k]
                h.kind, h.level, h.title = "heading", level, text
            else:
                clean = fix_text(title.replace("\r", "").strip()).replace("Copyright Page", "Copyright")
                self.report["missing_headings"].append((pno + 1, clean))
                y = min([i.y0 for i in items], default=0) - 1
                items.insert(0, Item("heading", y, y, level=level, title=clean, page=pno))
        # unnumbered bold run-in titles that are not in the TOC
        prev_hyph = False
        for it in items:
            t = plain(it.rows[0]) if it.kind == "para" else ""
            hyph = t.rstrip().endswith("-")
            if it.kind == "para" and self.is_heading_like(it.rows[0]) and len(t) < 90 \
                    and not CAPTION_RE.match(t) and not hyph and not prev_hyph:
                it.kind, it.level, it.title = "heading", 4, t
            prev_hyph = hyph
        items[:] = [i for i in items if i.kind != "drop"]
        items.sort(key=lambda i: (i.region, i.y0))

    @staticmethod
    def row_size(row):
        chs = [c for ln in row.lines for c in ln.chars if c.c.strip() and NOTE_OPEN not in c.c and IMG_OPEN not in c.c]
        return line_base(chs)[0] if chs else 0

    def head_levels(self):
        """Font sizes used for headings in a document without bookmarks -> level 1, 2, 3."""
        if hasattr(self, "_levels"):
            return self._levels
        cnt = collections.Counter()
        for pno in range(0, self.doc.page_count, max(1, self.doc.page_count // 150)):
            for l in raw_lines(self.doc[pno]):
                txt = "".join(c["c"] for sp in l["spans"] for c in sp["chars"]).strip()
                sp = [x for x in l["spans"] if x["text"].strip()]
                if not sp or not re.search(r"[A-Za-zÀ-ÿ]{2}", txt) or len(txt) > 150:
                    continue
                size = round(max(x["size"] for x in sp) * 2) / 2
                if size >= 1.12 * PF.body:
                    cnt[size] += 1
        sizes = sorted(cnt, reverse=True)
        self._levels = {sz: min(k + 1, 3) for k, sz in enumerate(sizes)}
        return self._levels

    def auto_headings(self, items, pno):
        """Headings of a PDF without bookmarks: larger type, or bold numbered lines."""
        levels = self.head_levels()
        paras = [i for i in items if i.kind == "para"]
        k = 0
        while k < len(paras):
            it = paras[k]
            row = it.rows[0]
            t = plain(row).strip()
            size = round(self.row_size(row) * 2) / 2
            big = size in levels and len(t) < 150 and re.search(r"[A-Za-zÀ-ÿ]{2}", t) \
                and not CAPTION_RE.match(t)
            numbered = re.match(r"^(\d+(\.\d+){0,3}|[IVX]+|[A-Z])\.?\s+[A-ZÀ-Ý]", t)
            if big or (numbered and self.is_heading_like(row) and len(t) < 100):
                level = levels.get(size, min(4, len(set(levels.values())) + 1)) if big else \
                    min(4, len(set(levels.values())) + 1 + (t.split()[0].count(".") if numbered else 0) // 2)
                # titles over several lines: same size, right below
                j = k + 1
                while big and j < len(paras) and round(self.row_size(paras[j].rows[0]) * 2) / 2 == size \
                        and paras[j].rows[0].bbox[1] - paras[j - 1].rows[0].bbox[3] < 0.8 * size:
                    t += " " + plain(paras[j].rows[0]).strip()
                    paras[j].kind = "drop"
                    j += 1
                it.kind, it.level, it.title = "heading", level, t
                k = j
                continue
            k += 1
        # digits alone in big type (chapter numbers) join the following title
        hs = [i for i in items if i.kind in ("heading", "para")]
        for a, b in zip(hs, hs[1:]):
            if a.kind == "heading" and re.fullmatch(r"[0-9IVX]+\.?", a.title) and b.kind == "heading":
                b.title = a.title + " " + b.title
                a.kind = "drop"
        self.apply_headings(items, pno, [])

    @staticmethod
    def slide_key(t):
        t = re.sub(r"\((cont|segue|continua|seguito)[^)]*\)|\b(cont|segue)\.?$|\(\d+\)|\b[IVX]+$|\d+/\d+|\.\.\.|…", "", t, flags=re.I)
        return Converter.norm(t)

    def slide_headings(self, items, pno, toc_here):
        """The title of a slide: big type at the top. Repeated titles (continued slides) are merged."""
        if toc_here:
            self.apply_headings(items, pno, toc_here)
        paras = [i for i in items if i.kind == "para"]
        h = self.doc[pno].rect.height
        title, rows = "", []
        for it in paras:
            row = it.rows[0]
            if row.bbox[1] > 0.3 * h:
                break
            size = self.row_size(row)
            if size >= 1.1 * PF.body or (not rows and self.is_heading_like(row) and row.bbox[1] < 0.2 * h):
                if rows and abs(size - self.row_size(rows[-1].rows[0])) > 1:
                    break
                rows.append(it)
                title += " " + plain(row).strip()
            elif rows:
                break
        title = title.strip()
        if not rows or not re.search(r"[A-Za-zÀ-ÿ]{2}", title):
            return
        key = self.slide_key(title)
        for it in rows[1:]:
            it.kind = "drop"
        if key == self.slide_title:
            rows[0].kind = "drop"
        else:
            self.slide_title = key
            rows[0].kind, rows[0].level, rows[0].title = "heading", 2, title
        items[:] = [i for i in items if i.kind != "drop"]

    @staticmethod
    def is_heading_like(row):
        chars = [c for ln in row.lines for c in ln.chars if c.c.strip()]
        if not chars:
            return False
        bold = sum(1 for c in chars if BOLD_RE.search(base_font(c.font)) or c.flags & 16)
        return bold / len(chars) > 0.8 and max(c.size for c in chars) >= 0.98 * PF.body

    # ------------------------------------------------------------ paragraphs
    def build(self, first, last):
        toc_by_page = collections.defaultdict(list)
        for level, title, p in self.toc:
            toc_by_page[p - 1].append((level, title))
        # pages to skip: title page (cover is rendered separately), printed contents,
        # analytic index (its page numbers mean nothing in an EPUB)
        skip = set()
        if self.toc and not PF.slides:
            skip.update(range(0, max(0, self.toc[0][2] - 1)))   # front matter before the first bookmark
        lvl1 = [(t, p - 1) for lv, t, p in self.toc if lv == 1]
        for (t, p), nxt in zip(lvl1, lvl1[1:] + [("", self.doc.page_count)]):
            if SKIP_SECTIONS.match(t.replace("\r", "").strip()):
                skip.update(range(p, nxt[1]))
        self.slide_title = None

        blocks = []        # list of dicts: {"kind","html"/..., "level"}
        open_para = None   # current paragraph being built: dict with runs and geometry
        pending_figs = []  # figures emitted while a paragraph may continue
        for pno in range(first, last + 1):
            if pno in skip:
                continue
            self.report["pages"] += 1
            items = self.page_items(pno)
            if not items and not self.page_notes:
                continue
            if PF.slides:
                open_para = self.close(blocks, open_para, pending_figs)   # a slide ends its text
                pending_figs = []
                self.slide_headings(items, pno, toc_by_page.get(pno, []))
            elif self.toc:
                self.apply_headings(items, pno, toc_by_page.get(pno, []))
            else:
                self.auto_headings(items, pno)
            prev_row = None
            region = None
            for it in items:
                left, right = it.left, it.right
                if it.region != region:
                    prev_row, region = None, it.region
                if it.kind == "heading":
                    open_para = self.close(blocks, open_para, pending_figs)
                    pending_figs = []
                    blocks.append({"kind": "h", "level": it.level, "title": it.title})
                    prev_row = None
                    continue
                if it.kind == "img":
                    if open_para and "fig" in it.html and not open_para["ended"]:
                        pending_figs.append(it.html)   # text may continue after the figure
                    else:
                        open_para = self.close(blocks, open_para, pending_figs)
                        pending_figs = []
                        blocks.append({"kind": "raw", "html": it.html})
                    prev_row = None
                    continue
                row = it.rows[0]
                t = plain(row)
                size = max(collections.Counter(round(c.size) for ln in row.lines for c in ln.chars).most_common(1))[0]
                indent = row.bbox[0] - left
                first_char = t.lstrip()[:1]
                new = True
                if open_para is not None:
                    if prev_row is not None:
                        gap = row.bbox[1] - prev_row.bbox[3]
                        same_x = abs(row.bbox[0] - prev_row.bbox[0]) <= 3 or \
                            abs(row.bbox[0] - open_para.get("tx", -1e9)) <= 0.4 * PF.body  # bullet continuation
                        # the previous line ended early: the first word of this row would have fitted there
                        chs = sorted((c for ln in row.lines for c in ln.chars), key=lambda c: c.bbox[0])
                        fw = next((c.bbox[0] for c in chs if c.c.isspace()), row.bbox[2]) - row.bbox[0]
                        short = (open_para["free"] > fw + 0.6 * PF.body
                                 or (open_para["short"] and open_para["ended"] and not first_char.islower())) \
                            and not (PF.slides and first_char.islower())
                        new = ((indent > 0.6 * PF.body and not same_x) or short or gap > 0.9 * PF.body
                               or size != open_para["size"]
                               or (CAPTION_RE.match(t) is not None and (open_para["ended"] or gap > 0.5 * PF.body))
                               or is_bullet(t))
                    else:
                        # first row on a page / after a figure: continue only mid-sentence
                        hang = abs(row.bbox[0] - open_para.get("tx", -1e9)) <= 0.4 * PF.body
                        new = ((indent > 0.6 * PF.body and not hang) or open_para["ended"] or not first_char.islower()
                               or size != open_para["size"])
                if new:
                    open_para = self.close(blocks, open_para, pending_figs)
                    pending_figs = []
                    cls = "caption" if CAPTION_RE.match(t) else ("small" if size < 0.95 * PF.body else "")
                    if is_bullet(t):
                        cls = "li"
                    elif not cls and (indent <= 0.6 * PF.body or PF.slides):
                        cls = "noind"
                    open_para = {"runs": [], "size": size, "cls": cls, "short": False, "ended": True}
                    if cls == "li":     # where the text after the bullet starts: continuation lines align there
                        xs = sorted((c.bbox[0] for ln in row.lines for c in ln.chars if c.c.isalnum()))
                        open_para["tx"] = xs[0] if xs else row.bbox[0]
                else:
                    self.join(open_para)
                open_para["runs"] += row_runs(row)
                # unmapped glyphs left in running text (dropped from the output)
                nbad = sum(c.c == PLACEHOLDER for ln in row.lines for c in ln.chars)
                if nbad:
                    self.report["ctrl"][pno + 1] += nbad
                open_para["short"] = row.bbox[2] < right - 1.5 * PF.body
                open_para["free"] = right - row.bbox[2]
                open_para["ended"] = bool(re.search(r"[.!?:;)]\s*$", t)) and not t.rstrip().endswith("-")
                prev_row = row
            for nb in self.page_notes:
                blocks.append(nb)
            # page break: the paragraph stays open for a possible continuation
        self.close(blocks, open_para, pending_figs)
        for b in blocks:
            if b["kind"] == "note":
                parts, p = [], {"runs": []}
                for pno, r in b.pop("rows"):
                    base = line_base([c for ln in r.lines for c in ln.chars if c.c.strip()])[0]
                    extra_small = len(r.lines) > 1 and any(
                        line_base(ln.chars)[0] < 0.75 * base for ln in r.lines)
                    if stacked(r) or extra_small:     # fractions etc.: keep the math as a picture
                        if p["runs"]:
                            parts.append(runs_to_html(p["runs"]))
                            p = {"runs": []}
                        name, w, _ = self.render(pno, r.bbox, "eq")
                        parts.append(f' <img class="noteeq" src="img/{name}" alt="{html.escape(plain(r), quote=True)}"'
                                     f' style="width:{w / PF.note:.2f}em"/> ')
                        continue
                    if p["runs"]:
                        self.join(p)
                    p["runs"] += row_runs(r)
                if p["runs"]:
                    parts.append(runs_to_html(p["runs"]))
                b["html"] = "".join(parts).strip()
        return blocks

    @staticmethod
    def join(p):
        """Join the next line: drop the hyphen when the word was split."""
        last = p["runs"][-1]
        if last[0].rstrip().endswith("-") and not last[0].rstrip().endswith("--"):
            last[0] = last[0].rstrip()[:-1]
            p["dehyph"] = True
        else:
            last[0] = last[0].rstrip() + " "

    @staticmethod
    def close(blocks, p, figs):
        if p is not None and p["runs"]:
            blocks.append({"kind": "p", "cls": p["cls"], "html": runs_to_html(p["runs"])})
        for f in figs:
            blocks.append({"kind": "raw", "html": f})
        return None


# ---------------------------------------------------------------- EPUB writer
CSS = """
body { font-family: serif; line-height: 1.4; margin: 0 0.4em; }
p { margin: 0; text-indent: 1.3em; text-align: justify; }
h1 + p, h2 + p, h3 + p, h4 + p, p.noind { text-indent: 0; }
p.small { font-size: 0.9em; margin: 0.4em 0; text-indent: 0; }
p.caption { font-size: 0.88em; margin: 0.3em 0 1em 0; text-indent: 0; }
h1 { font-size: 1.5em; margin: 1.5em 0 1em 0; }
h2 { font-size: 1.25em; margin: 1.3em 0 0.6em 0; }
h3 { font-size: 1.1em; margin: 1.1em 0 0.5em 0; }
h4 { font-size: 1em; margin: 1em 0 0.4em 0; }
div.eq { text-align: center; margin: 0.6em 0; }
div.eq img { max-width: 100%; }
div.fig { text-align: center; margin: 1em 0 0.3em 0; page-break-inside: avoid; }
div.fig img { max-width: 100%; }
sup, sub { font-size: 0.72em; line-height: 0; }
sup.noteref a { text-decoration: none; font-weight: bold; }
section.footnotes { margin-top: 2em; font-size: 0.88em; }
aside.footnote p { text-indent: 0; margin: 0.4em 0; }
img.noteeq { vertical-align: middle; max-width: 100%; }
img.im { display: inline; max-width: 100%; }
p.li { text-indent: -0.9em; margin: 0.25em 0 0.25em 1.2em; text-align: left; }
"""


def xhtml(title, body):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            f'xml:lang="{PF.lang}" lang="{PF.lang}">\n<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>'
            f'<title>{html.escape(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            f'<body>\n{body}\n</body>\n</html>\n')


def write_epub(blocks, images, cover_png, out, meta):
    chapters = []   # (title, [html lines], [(level, id, title)])
    hid = 0
    for b in blocks:
        # new file at every h1, or wherever a block asks for it (split)
        if b["kind"] == "h" and (b["level"] == 1 or b.get("split")) or not chapters:
            chapters.append([b.get("title", meta["title"]) if b["kind"] == "h" else meta["title"], [], []])
        ch = chapters[-1]
        if b["kind"] == "h":
            hid += 1
            lv = min(b["level"], 4)
            ch[1].append(f'<h{lv} id="h{hid}">{b.get("html") or html.escape(b["title"])}</h{lv}>')
            if lv <= meta.get("nav_depth", 3):
                ch[2].append((lv, f"h{hid}", b["title"]))
        elif b["kind"] == "note":
            if len(ch) < 4:
                ch.append([])
            ch[3].append(b)
        elif b["kind"] == "p":
            cls = f' class="{b["cls"]}"' if b["cls"] else ""
            ch[1].append(f"<p{cls}>{b['html']}</p>")
        else:
            ch[1].append(b["html"])

    book_id = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, meta["title"]))
    z = zipfile.ZipFile(out, "w")
    z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
    zc = dict(compress_type=zipfile.ZIP_DEFLATED)
    z.writestr("META-INF/container.xml",
               '<?xml version="1.0"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
               '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
               '</rootfiles></container>', **zc)
    z.writestr("OEBPS/style.css", CSS, **zc)
    manifest = ['<item id="css" href="style.css" media-type="text/css"/>',
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="cover-img" href="img/cover.png" media-type="image/png" properties="cover-image"/>',
                '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>']
    spine = ['<itemref idref="cover" linear="yes"/>']
    z.writestr("OEBPS/img/cover.png", cover_png, **zc)
    z.writestr("OEBPS/cover.xhtml", xhtml(meta["title"],
               '<div style="text-align:center"><img src="img/cover.png" alt="Copertina" style="max-width:100%;max-height:100%"/></div>'), **zc)
    for name, data in images.items():
        mt = {"png": "image/png", "gif": "image/gif", "svg": "image/svg+xml"}.get(name.rsplit(".", 1)[-1], "image/jpeg")
        manifest.append(f'<item id="i_{name.replace(".", "_")}" href="img/{name}" media-type="{mt}"/>')
        z.writestr(f"OEBPS/img/{name}", data, **zc)
    nav_items, ncx_points = [], []
    order = 0
    # the nav tree is shared by all files, so a section can span several of them
    stack = [nav_items]
    levels = [0]
    for n, (title, body, heads, *rest) in enumerate(chapters, 1):
        fn = f"ch{n:02d}.xhtml"
        if rest and rest[0]:
            body = body + ['<section class="footnotes" epub:type="footnotes"><hr/>'] + [
                f'<aside epub:type="footnote" id="fn{nb["id"]}" class="footnote"><p>'
                f'<a href="#fnr{nb["id"]}">{nb["num"]}</a> {nb["html"]}</p></aside>' for nb in rest[0]] + ["</section>"]
        z.writestr(f"OEBPS/{fn}", xhtml(title, "\n".join(body)), **zc)
        props = ' properties="mathml"' if "<math" in "".join(body) else ""
        manifest.append(f'<item id="c{n}" href="{fn}" media-type="application/xhtml+xml"{props}/>')
        spine.append(f'<itemref idref="c{n}"/>')
        for lv, hid_, t in heads:
            while levels[-1] >= lv:
                stack.pop()
                levels.pop()
            node = {"t": t, "href": f"{fn}#{hid_}", "kids": []}
            stack[-1].append(node)
            stack.append(node["kids"])
            levels.append(lv)
            order += 1
            ncx_points.append((order, t, f"{fn}#{hid_}"))
        if not heads:
            nav_items.append({"t": title, "href": fn, "kids": []})

    def ol(nodes):
        if not nodes:
            return ""
        return "<ol>" + "".join(
            f'<li><a href="{html.escape(x["href"])}">{html.escape(x["t"])}</a>{ol(x["kids"])}</li>' for x in nodes) + "</ol>"
    z.writestr("OEBPS/nav.xhtml", xhtml("Indice", f'<nav epub:type="toc" id="toc"><h1>Indice</h1>{ol(nav_items)}</nav>'), **zc)
    ncx = "".join(f'<navPoint id="n{o}" playOrder="{o}"><navLabel><text>{html.escape(t)}</text></navLabel>'
                  f'<content src="{html.escape(h)}"/></navPoint>' for o, t, h in ncx_points)
    z.writestr("OEBPS/toc.ncx", '<?xml version="1.0" encoding="utf-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
               f'<head><meta name="dtb:uid" content="{book_id}"/></head><docTitle><text>{html.escape(meta["title"])}</text></docTitle>'
               f'<navMap>{ncx}</navMap></ncx>', **zc)
    creators = "".join(f'<dc:creator id="a{i}">{html.escape(a)}</dc:creator>' for i, a in enumerate(meta["authors"]))
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{meta.get("lang", PF.lang)}">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="bookid">{book_id}</dc:identifier><dc:title>{html.escape(meta["title"])}</dc:title>'
           f'<dc:language>{meta.get("lang", PF.lang)}</dc:language>{creators}'
           + (f'<dc:publisher>{html.escape(meta["publisher"])}</dc:publisher>' if meta.get("publisher") else "")
           + (f'<dc:date>{meta["date"]}</dc:date>' if meta.get("date") else "")
           + '<meta name="cover" content="cover-img"/>'
           '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>'
           f'<manifest>{"".join(manifest)}</manifest><spine toc="ncx">{"".join(spine)}</spine></package>')
    z.writestr("OEBPS/content.opf", opf, **zc)
    z.close()


def clean_meta(t):
    t = (t or "").strip()
    if not t or any(ord(c) < 32 for c in t) or (" " not in t and re.search(r"[_\-.]", t)) or re.search(r"\.(dvi|tex|docx?|pptx?|indd|qxd)$|^untitled|^microsoft|^powerpoint", t, re.I):
        return ""
    return t


def guess_title(doc, path):
    t = clean_meta(doc.metadata.get("title"))
    if t and len(t) > 3:
        return t
    # largest type on the first pages
    best = (0, "")
    for pno in range(min(3, doc.page_count)):
        for l in raw_lines(doc[pno]):
            sp = [x for x in l["spans"] if x["text"].strip()]
            txt = " ".join(x["text"].strip() for x in sp)
            if sp and re.search(r"[A-Za-zÀ-ÿ]{3}", txt) and len(txt) < 150 and not is_pagenum(txt):
                size = max(x["size"] for x in sp)
                if size > best[0] + 0.5:
                    best = (size, txt)
        if best[1]:
            break
    import os
    return best[1] or os.path.splitext(os.path.basename(path))[0]


def main():
    import os
    ap = argparse.ArgumentParser(description="Scientific PDF (book, article, slides) -> EPUB3")
    ap.add_argument("files", nargs="+", help="INPUT.pdf [MORE.pdf ...] OUTPUT.epub")
    ap.add_argument("--pages", help="1-based range, e.g. 59-86 (default: whole document; single PDF only)")
    ap.add_argument("--report")
    ap.add_argument("--title")
    ap.add_argument("--authors", help='"Name One;Name Two"')
    ap.add_argument("--publisher", default="")
    ap.add_argument("--lang", help="it, en, ... (default: detected)")
    ap.add_argument("--slides", action="store_true", default=None, help="force slide mode")
    ap.add_argument("--pages-mode", dest="slides", action="store_false", help="force book/article mode")
    a = ap.parse_args()
    if len(a.files) < 2 or not a.files[-1].lower().endswith(".epub"):
        ap.error("give one or more PDFs followed by the output .epub")
    pdfs, out = a.files[:-1], a.files[-1]
    images, blocks, reports, covers = {}, [], [], []
    meta_title = a.title
    authors = a.authors.split(";") if a.authors else None
    for n, pdf in enumerate(pdfs):
        cv = Converter(pdf, images, prefix=f"d{n + 1}_" if len(pdfs) > 1 else "")
        if a.pages and len(pdfs) == 1:
            f, l = (int(x) for x in a.pages.split("-"))
        else:
            f, l = 1, cv.doc.page_count
        analyze(cv.doc, range(f - 1, l))
        if a.slides is not None:
            PF.slides = a.slides
        if a.lang:
            PF.lang = a.lang
        title = guess_title(cv.doc, pdf)
        if len(pdfs) > 1 or (PF.slides and not cv.toc):
            # one chapter per PDF (slide decks): its title, then one section per slide title
            blocks.append({"kind": "h", "level": 1, "title": title})
        b = cv.build(f - 1, l - 1)
        if PF.slides and len(b) > 1 and b[0]["kind"] == "h" and blocks and blocks[-1]["kind"] == "h" \
                and Converter.slide_key(b[0]["title"]) == Converter.slide_key(title):
            b = b[1:]
        blocks += b
        title_page = next((p - 1 for lv, t, p in cv.toc if t.strip() == "Title Page"), 0)
        covers.append(cv.doc[title_page].get_pixmap(matrix=fitz.Matrix(2, 2) if not PF.slides else fitz.Matrix(1, 1)).tobytes("png"))
        if meta_title is None:
            meta_title = title if len(pdfs) == 1 else os.path.splitext(os.path.basename(out))[0]
        if authors is None:
            au = clean_meta(cv.doc.metadata.get("author"))
            authors = [x.strip() for x in re.split(r";|,| and | e ", au) if x.strip()] if au else ["—"]
        rep = cv.report
        reports.append({
            "pdf": pdf, "mode": "slides" if PF.slides else "pages", "lang": PF.lang,
            "body_pt": PF.body, "note_pt": PF.note,
            "running_heads": len(PF.running), "two_column_pages": rep.get("two_column_pages", 0),
            "pages_converted": rep["pages"], "equation_images": rep["eq_imgs"],
            "figure_images": rep["fig_imgs"], "paragraphs": sum(1 for x in b if x["kind"] == "p"),
            "headings": sum(1 for x in b if x["kind"] == "h"),
            "inline_math_images": rep.get("inline_math", 0),
            "math_glyphs": rep.get("math_glyphs", 0),
            "math_glyphs_not_in_output": rep.get("math_lost", 0),
            "pages_with_lost_math": dict(sorted(rep.get("math_lost_pages", {}).items())),
            "pages_with_control_chars": dict(sorted(rep["ctrl"].items())),
            "headings_not_found_on_page": rep["missing_headings"],
            "footnotes": len(rep["notes"]),
            "footnotes_without_reference": [x for x in rep["notes"] if x[2] != "trovato"]})
    write_epub(blocks, images, covers[0], out, {"title": meta_title, "authors": authors,
                                                "publisher": a.publisher, "lang": PF.lang})
    summary = reports[0] if len(reports) == 1 else {"documents": reports}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"-> {out} ({os.path.getsize(out) / 1e6:.1f} MB)")
    if a.report:
        with open(a.report, "w") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
