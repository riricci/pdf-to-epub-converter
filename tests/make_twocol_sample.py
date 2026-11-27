#!/usr/bin/env python3
"""Build a synthetic two-column scientific article (PDF) to test pdf2epub:
running head + page numbers, title/abstract across the page, two columns,
numbered sections without bookmarks, footnotes with raised markers, inline math
with sub/superscripts, a numbered display equation and a fraction, a figure.

Usage: make_twocol_sample.py OUT.pdf
"""
import re
import sys

import fitz

SUP = "/System/Library/Fonts/Supplemental/"
FONTS = {"r": SUP + "Times New Roman.ttf", "i": SUP + "Times New Roman Italic.ttf",
         "b": SUP + "Times New Roman Bold.ttf", "m": SUP + "STIXTwoMath.otf"}
W, H = 595, 842
COLS = [(50, 290), (305, 545)]
TOP, BOTTOM = 80, 780
BODY, LEAD = 10, 12.2

LOREM = ("The measurement of the cross section requires a careful treatment of the detector "
         "acceptance and of the background, which is estimated from side bands of the invariant "
         "mass distribution. The systematic uncertainties are dominated by the luminosity and by "
         "the energy scale of the calorimeter, both evaluated with independent control samples.")

DOC = [
    ("h", "1 Introduction"),
    ("p", "Particle detectors measure the energy $E$ and the momentum $p$ of the products of a "
          "collision[^1]. In natural units the invariant mass is $m^{2} = E^{2} − p^{2}$, and the "
          "Lorentz factor $γ$ follows from $β = p/E$. " + LOREM),
    ("p", LOREM + " The resolution improves as $σ_{E}/E ∝ 1/√E$ at high energy."),
    ("eq", "E_{tot} = ∑_{i} m_{i} c^{2}", "(1)"),
    ("p", "where the sum runs over all the particles of the event. " + LOREM),
    ("h", "2 Method"),
    ("p", "The decay width is obtained from the lifetime through $Γ = ħ/τ$, as discussed in "
          "standard textbooks[^2]. " + LOREM + " " + LOREM),
    ("frac", "Γ", "ħ", "τ", "(2)"),
    ("p", "The ratio in Eq. (2) is then compared with the prediction. " + LOREM),
    ("fig", "Figure 1. Invariant mass distribution of the selected candidates."),
    ("p", LOREM + " " + LOREM),
    ("h", "2.1 Event selection"),
    ("p", "Events are selected with at least two tracks with transverse momentum "
          "$p_{T} > 20$ GeV. " + LOREM + " " + LOREM),
    ("h", "3 Results"),
    ("p", LOREM + " The fitted mass is $m = 91.19 ± 0.02$ GeV, in agreement with the "
          "world average[^3]. " + LOREM),
    ("p", LOREM + " " + LOREM + " " + LOREM),
    ("h", "4 Conclusions"),
    ("p", LOREM + " " + LOREM),
]
NOTES = {"1": "In a fixed-target experiment only the forward hemisphere is instrumented.",
         "2": "See for example the review of particle physics, section on kinematics.",
         "3": "The average includes the measurements of the four LEP experiments."}


def runs(text, size=BODY, font="r"):
    """markup -> [(text, font, size, rise)]"""
    out = []
    for part in re.split(r"(\$[^$]+\$|\[\^\d+\])", text):
        if not part:
            continue
        if part.startswith("[^"):
            out.append((part[2:-1], "r", size * 0.62, -0.38 * size))
        elif part.startswith("$"):
            for t in re.split(r"([_^]\{[^}]*\})", part[1:-1]):
                if t.startswith("^{"):
                    out.append((t[2:-1], "m", size * 0.7, -0.38 * size))
                elif t.startswith("_{"):
                    out.append((t[2:-1], "m", size * 0.7, 0.22 * size))
                elif t:
                    out.append((t, "m", size, 0))
        else:
            out.append((part, font, size, 0))
    return out


class Writer:
    def __init__(self):
        self.doc = fitz.open()
        self.fonts = {k: fitz.Font(fontfile=v) for k, v in FONTS.items()}
        self.page = None
        self.col = 0
        self.y = 0
        self.notes = []     # notes waiting for the bottom of the current column

    def new_page(self):
        self.page = self.doc.new_page(width=W, height=H)
        for k, v in FONTS.items():
            self.page.insert_font(fontname="F" + k, fontfile=v)
        n = self.doc.page_count
        self.text(50, 50, "Journal of Synthetic Tests 12 (2026) 1–3", "i", 8)
        self.text(W / 2 - 3, 810, str(n), "r", 9)
        self.col, self.y = 0, TOP

    def text(self, x, y, s, font, size):
        self.page.insert_text((x, y), s, fontname="F" + font, fontsize=size)
        return self.fonts[font].text_length(s, fontsize=size)

    def width(self, rs):
        return sum(self.fonts[f].text_length(t, fontsize=s) for t, f, s, _ in rs)

    def bottom(self):
        return BOTTOM - len(self.notes) * 11 - (8 if self.notes else 0)

    def advance(self, h):
        if self.y + h > self.bottom():
            self.flush_notes()
            if self.col == 0:
                self.col, self.y = 1, self.top_of_col
            else:
                self.new_page()
                self.top_of_col = TOP
        self.y += h

    def flush_notes(self):
        if not self.notes:
            return
        x0, x1 = COLS[self.col]
        y = BOTTOM - len(self.notes) * 11 + 4
        self.page.draw_line((x0, y - 9), (x0 + 60, y - 9), width=0.4)
        for num, t in self.notes:
            self.text(x0, y - 3, num, "r", 5.5)
            self.text(x0 + 5, y, t[:62], "r", 8)
            y += 11
        self.notes = []

    def para(self, text, indent=True):
        # words = lists of runs without spaces
        words, cur = [], []
        for t, f, s, r in runs(text):
            parts = re.split(r"(\s+)", t)
            for p in parts:
                if not p:
                    continue
                if p.isspace():
                    if cur:
                        words.append(cur)
                    cur = []
                else:
                    cur.append((p, f, s, r))
        if cur:
            words.append(cur)
        space = self.fonts["r"].text_length(" ", fontsize=BODY)
        lines, line, lw = [], [], 0
        x0, x1 = COLS[self.col]
        avail = (x1 - x0) - (12 if indent else 0)
        for wd in words:
            ww = self.width(wd)
            if line and lw + space + ww > avail:
                lines.append(line)
                line, lw, avail = [], 0, x1 - x0
            lw += (space if line else 0) + ww
            line.append(wd)
        if line:
            lines.append(line)
        for k, line in enumerate(lines):
            self.advance(LEAD)
            x0, x1 = COLS[self.col]
            x = x0 + (12 if k == 0 and indent else 0)
            for wd in line:
                for t, f, s, r in wd:
                    if r < 0 and f == "r" and t in NOTES:
                        self.notes.append((t, NOTES[t]))
                    x += self.text(x, self.y + r, t, f, s)
                x += space
        self.y += 3

    def heading(self, t):
        self.advance(LEAD + 6)
        self.text(COLS[self.col][0], self.y, t, "b", 10.5)
        self.y += 3

    def equation(self, tex, num):
        self.advance(LEAD + 10)
        x0, x1 = COLS[self.col]
        rs = runs("$" + tex + "$")
        x = (x0 + x1) / 2 - self.width(rs) / 2
        for t, f, s, r in rs:
            x += self.text(x, self.y + r, t, f, s)
        self.text(x1 - 14, self.y, num, "r", BODY)
        self.y += 10

    def fraction(self, lhs, num, den, eqn):
        self.advance(LEAD + 24)
        x0, x1 = COLS[self.col]
        cx = (x0 + x1) / 2
        y = self.y - 8
        self.text(cx - 30, y + 3, lhs + " =", "m", BODY)
        self.text(cx + 2, y - 4, num, "m", BODY)
        self.page.draw_line((cx - 1, y), (cx + 10, y), width=0.5)
        self.text(cx + 2, y + 10, den, "m", BODY)
        self.text(x1 - 14, y + 3, eqn, "r", BODY)
        self.y += 8

    def figure(self, cap):
        self.advance(110)
        x0, x1 = COLS[self.col]
        r = fitz.Rect(x0 + 20, self.y - 100, x1 - 20, self.y - 12)
        self.page.draw_rect(r, color=(0, 0, 0), width=0.6)
        pts = [(r.x0 + k * (r.width / 20), r.y1 - 4 - 70 * (2.7 ** (-((k - 10) / 3) ** 2))) for k in range(21)]
        self.page.draw_polyline(pts, color=(0.1, 0.2, 0.7), width=1)
        self.text(r.x0 + 3, r.y0 + 9, "events", "r", 6)
        self.text(r.x1 - 35, r.y1 - 3, "m [GeV]", "r", 6)
        self.y -= 4
        self.para(cap, indent=False)


def main(out):
    w = Writer()
    w.new_page()
    # title block across the page
    w.text(80, 100, "A synthetic measurement of the Z boson line shape", "b", 17)
    w.text(200, 122, "A. Tester, B. Checker", "r", 11)
    abstract = ("Abstract. We describe a synthetic analysis used to test the conversion of two-column "
                "scientific articles. The text contains footnotes, inline formulas, numbered display "
                "equations, a fraction and a figure, laid out in two columns below a title and an "
                "abstract that span the whole page width.")
    y = 150
    line = ""
    for word in abstract.split():
        if w.fonts["i"].text_length(line + " " + word, fontsize=9.5) > 445:
            w.text(75, y, line.strip(), "i", 9.5)
            y += 11.5
            line = ""
        line += " " + word
    w.text(75, y, line.strip(), "i", 9.5)
    w.top_of_col = y + 25
    w.y = w.top_of_col
    for blk in DOC:
        k = blk[0]
        if k == "h":
            w.heading(blk[1])
        elif k == "p":
            w.para(blk[1])
        elif k == "eq":
            w.equation(blk[1], blk[2])
        elif k == "frac":
            w.fraction(*blk[1:])
        elif k == "fig":
            w.figure(blk[1])
    w.flush_notes()
    w.doc.set_metadata({"title": "A synthetic measurement of the Z boson line shape",
                        "author": "A. Tester; B. Checker"})
    w.doc.save(out)
    print(out, w.doc.page_count, "pages")


if __name__ == "__main__":
    main(sys.argv[1])
