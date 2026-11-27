#!/usr/bin/env python3
"""Dispense in Markdown leggero + LaTeX -> EPUB3 con formule in MathML.

Sintassi delle sorgenti (slides/src/*.md):
  # Capitolo / ## Sezione / ### Sottosezione / #### Paragrafo
  testo con $formula$ in linea, **grassetto**, *corsivo*
  $$ formula in display $$   (anche su più righe, anche \\begin{aligned}...)
  - elenco puntato          1. elenco numerato
  | a | b | c |             tabella (la prima riga è l'intestazione)
  > riquadro                (il contenuto segue la stessa sintassi)
  @fig ID [ID ...] | didascalia   (ID = nome del PNG in --figdir, senza estensione)

Uso: md2epub.py OUT.epub src1.md [src2.md ...] --title T --author A [--subtitle S]
"""
import argparse
import html
import os
import re
import sys

import fitz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdf2epub  # noqa: E402
from tex2mathml import tex2mathml, GREEK, OPS  # noqa: E402

CSS_EXTRA = """
body { font-family: "Iowan Old Style", "Palatino", "Georgia", serif; }
h1, h2, h3, h4 { font-family: "Avenir Next", "Helvetica Neue", "Helvetica", sans-serif; color: #1a3d6d; }
h1 { border-bottom: 2px solid #1a3d6d; padding-bottom: 0.2em; }
p { margin: 0.45em 0; text-indent: 0; text-align: left; }
ul, ol { margin: 0.4em 0 0.6em 0; padding-left: 1.6em; }
li { margin: 0.2em 0; }
math[display="block"] { display: block; margin: 0.7em auto; text-align: center; }
div.math { margin: 0.7em 0; text-align: center; overflow-x: auto; overflow-y: hidden; }
div.math math { display: inline-block; }
math { font-family: "STIX Two Math", "Latin Modern Math", "Cambria Math", math; font-size: 1.08em; }
div.box { border-left: 4px solid #1a3d6d; background: #eef3fa; padding: 0.4em 0.8em; margin: 1em 0; }
div.box p:first-child { margin-top: 0.2em; }
div.fig { text-align: center; margin: 1.1em 0 0.3em 0; page-break-inside: avoid; }
div.fig img { max-width: 100%; height: auto; }
div.fig.multi img { max-width: 48%; margin: 0 0.5%; vertical-align: middle; }
p.caption { font-size: 0.88em; color: #444; text-align: center; margin: 0.3em 1em 1.1em 1em; }
table { border-collapse: collapse; margin: 0.9em auto; font-size: 0.92em; }
th, td { border: 1px solid #9aa9bd; padding: 0.25em 0.5em; vertical-align: top; }
th { background: #e3eaf4; }
"""

FIG_MAX_W = 1300


def tex_plain(t):
    """testo con formule -> testo semplice (indice, alt delle immagini)"""
    def conv(m):
        x = m.group(1)
        x = re.sub(r"\\(" + "|".join(sorted(GREEK, key=len, reverse=True)) + r")(?![A-Za-z])", lambda g: GREEK[g.group(1)], x)
        x = re.sub(r"\\(" + "|".join(sorted(OPS, key=len, reverse=True)) + r")(?![A-Za-z])", lambda g: OPS[g.group(1)], x)
        x = re.sub(r"\\(bar|overline)\s*\{?([A-Za-z])\}?", lambda g: g.group(2) + "\u0304", x)
        x = re.sub(r"\\(mathcal|mathrm|text|mathbf)\s*", "", x)
        x = re.sub(r"\\[,;:! ]", " ", x)
        x = re.sub(r"[{}^_\\]", "", x)
        return x.replace("{,}", ",")
    t = re.sub(r"(?<!\\)\$(.+?)(?<!\\)\$", conv, t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    return re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)


def inline(s):
    maths = []

    def keep(m):
        maths.append(tex2mathml(m.group(1)))
        return f"\x00{len(maths) - 1}\x00"
    s = re.sub(r"(?<!\\)\$(.+?)(?<!\\)\$", keep, s)
    e = html.escape(s, quote=False)
    e = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", e)
    e = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", e)
    e = e.replace("\\$", "$")
    return re.sub("\x00(\\d+)\x00", lambda m: maths[int(m.group(1))], e)


class Book:
    def __init__(self, figdir):
        self.figdir = figdir
        self.images = {}
        self.blocks = []

    def image(self, fid):
        if fid in self.images.values():
            pass
        src = os.path.join(self.figdir, fid + ".png")
        pix = fitz.Pixmap(src)
        if pix.alpha:
            pix = fitz.Pixmap(pix, 0)
        if pix.width > FIG_MAX_W:
            f = FIG_MAX_W / pix.width
            pix = fitz.Pixmap(pix, int(pix.width * f), int(pix.height * f))
        png = pix.tobytes("png")
        jpg = pix.tobytes("jpg", jpg_quality=85)
        if len(png) <= 1.4 * len(jpg):
            name, data = fid + ".png", png
        else:
            name, data = fid + ".jpg", jpg
        self.images[name] = data
        return name, pix.width

    def fig(self, ids, caption):
        imgs = []
        for fid in ids:
            name, w = self.image(fid)
            # larghezza in em: circa 1 em ogni 16 px di una slide resa a 2,5x (1 pt ≈ 2,5 px)
            em = min(w / 2.5 / 12, 40)
            imgs.append(f'<img src="img/{name}" alt="{html.escape(tex_plain(caption) or fid, quote=True)}" style="width:{em:.1f}em"/>')
        cls = "fig multi" if len(imgs) > 1 else "fig"
        h = f'<div class="{cls}">{"".join(imgs)}</div>'
        if caption:
            h += f'<p class="caption">{inline(caption)}</p>'
        return h

    def parse(self, lines):
        """restituisce una lista di blocchi html (e aggiunge i titoli a self.blocks)"""
        out = []
        i = 0
        para = []

        def flush():
            if para:
                out.append(("p", "<p>" + inline(" ".join(para)) + "</p>"))
                para.clear()

        while i < len(lines):
            ln = lines[i].rstrip()
            st = ln.strip()
            if not st:
                flush()
                i += 1
                continue
            m = re.match(r"(#{1,4})\s+(.*)", st)
            if m:
                flush()
                out.append(("h", len(m.group(1)), m.group(2)))
                i += 1
                continue
            if st.startswith("$$"):
                flush()
                buf = st[2:]
                while not (buf.rstrip().endswith("$$") and len(buf.strip()) >= 2) and i + 1 < len(lines):
                    i += 1
                    buf += "\n" + lines[i]
                tex = buf.rstrip()[:-2]
                out.append(("x", '<div class="math">' + tex2mathml(tex, display=True) + "</div>"))
                i += 1
                continue
            if st.startswith("@fig"):
                flush()
                spec, _, cap = st[4:].partition("|")
                out.append(("x", self.fig(spec.split(), cap.strip())))
                i += 1
                continue
            if st.startswith(">"):
                flush()
                inner = []
                while i < len(lines) and lines[i].strip().startswith(">"):
                    inner.append(re.sub(r"^\s*>\s?", "", lines[i]))
                    i += 1
                sub = self.parse(inner)
                out.append(("x", '<div class="box">' + "".join(b[1] for b in sub if b[0] != "h") + "</div>"))
                continue
            if st.startswith("|"):
                flush()
                rows = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    cells = [c.strip() for c in lines[i].strip().strip("|").split(" | ")]
                    rows.append(cells)
                    i += 1
                h = ["<table>"]
                for r, cells in enumerate(rows):
                    tag = "th" if r == 0 else "td"
                    h.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
                h.append("</table>")
                out.append(("x", "".join(h)))
                continue
            m = re.match(r"(-|\d+\.)\s+", st)
            if m:
                flush()
                ordered = m.group(1) != "-"
                items = []
                while i < len(lines):
                    s2 = lines[i].strip()
                    m2 = re.match(r"(-|\d+\.)\s+(.*)", s2)
                    if m2 and (m2.group(1) != "-") == ordered:
                        items.append(m2.group(2))
                    elif s2 and items and not re.match(r"(-|\d+\.)\s|[#@|>]|\$\$", s2):
                        items[-1] += " " + s2
                    else:
                        break
                    i += 1
                tag = "ol" if ordered else "ul"
                out.append(("x", f"<{tag}>" + "".join(f"<li>{inline(t)}</li>" for t in items) + f"</{tag}>"))
                continue
            para.append(st)
            i += 1
        flush()
        return out

    def add_file(self, path):
        lines = open(path, encoding="utf-8").read().split("\n")
        for b in self.parse(lines):
            if b[0] == "h":
                _, lv, t = b
                self.blocks.append({"kind": "h", "level": lv, "title": tex_plain(t),
                                    "html": inline(t), "split": lv == 1})
            else:
                self.blocks.append({"kind": "x", "html": b[1]})


def make_cover(title, subtitle, author):
    doc = fitz.open()
    page = doc.new_page(width=600, height=900)
    page.draw_rect(page.rect, color=None, fill=(0.10, 0.24, 0.43))
    page.draw_rect(fitz.Rect(0, 600, 600, 606), color=None, fill=(0.95, 0.75, 0.25))
    page.insert_textbox(fitz.Rect(50, 260, 550, 470), title, fontsize=46, fontname="hebo",
                        color=(1, 1, 1), align=1)
    page.insert_textbox(fitz.Rect(50, 480, 550, 580), subtitle, fontsize=22, fontname="helv",
                        color=(0.85, 0.9, 1), align=1)
    page.insert_textbox(fitz.Rect(50, 640, 550, 700), author, fontsize=22, fontname="helv",
                        color=(1, 1, 1), align=1)
    return page.get_pixmap().tobytes("png")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("epub")
    ap.add_argument("sources", nargs="+")
    ap.add_argument("--title", required=True)
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--author", required=True)
    ap.add_argument("--figdir", default="slides/fig")
    ap.add_argument("--date", default="2026")
    a = ap.parse_args()
    book = Book(a.figdir)
    for s in a.sources:
        book.add_file(s)
    pdf2epub.CSS = pdf2epub.CSS + CSS_EXTRA
    meta = {"title": a.title, "authors": [a.author], "publisher": "Appunti dalle slide del corso",
            "date": a.date, "nav_depth": 3}
    pdf2epub.write_epub(book.blocks, book.images, make_cover(a.title, a.subtitle, a.author), a.epub, meta)
    size = os.path.getsize(a.epub)
    nh = sum(1 for b in book.blocks if b["kind"] == "h")
    nm = sum(b["html"].count("<math") for b in book.blocks if b["kind"] == "x") + \
        sum(b["html"].count("<math") for b in book.blocks if b["kind"] == "h")
    print(f"{a.epub}: {size/1e6:.1f} MB, {nh} titoli, {nm} formule, {len(book.images)} figure")


if __name__ == "__main__":
    main()
