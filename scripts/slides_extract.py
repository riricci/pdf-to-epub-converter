#!/usr/bin/env python3
"""Estrae dalle slide (PDF esportati da PowerPoint):
  - le figure (raster + vettoriali + etichette vicine) come PNG ritagliati
  - contact sheet 2x2 con numeri slide e riquadri delle figure (per la revisione)
  - un indice JSON delle figure

Uso: slides_extract.py OUTDIR deck1.pdf [deck2.pdf ...]
"""
import fitz, json, os, re, sys

FRAME_MIN = 700          # rettangolo di sfondo della slide (39,11,803,584)
MIN_W, MIN_H = 40, 30    # dimensione minima di una figura (pt)
FIG_ZOOM = 2.5
# ritocchi manuali per figura (coordinate in pt della slide):
#   {"1-168-1": {"mask": [[x0,y0,x1,y1], ...], "clip": [x0,y0,x1,y1]}}
OVERRIDES = {}
ARIAL = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
ARIAL_B = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def touch(a, b, tol):
    return not (a.x1 + tol < b.x0 or b.x1 + tol < a.x0 or a.y1 + tol < b.y0 or b.y1 + tol < a.y0)


def merge(rects, tol):
    rects = [fitz.Rect(r) for r in rects]
    changed = True
    while changed:
        changed = False
        out = []
        for r in rects:
            for j, o in enumerate(out):
                if touch(o, r, tol):
                    out[j] = o | r
                    changed = True
                    break
            else:
                out.append(fitz.Rect(r))
        rects = out
    return rects


def text_spans(page):
    """(rect, testo, is_prose): prosa = Comic Sans / Cambria Math in corpo grande."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            # se nella riga c'è prosa, anche i pedici/apici della riga sono prosa
            line_prose = any(sp["size"] >= 19 and ("Comic" in sp["font"] or "Cambria" in sp["font"])
                             and sp["text"].strip() for sp in l["spans"])
            ltext = "".join(sp["text"] for sp in l["spans"]).strip()
            for s in l["spans"]:
                s["_line"] = (fitz.Rect(l["bbox"]), ltext)
                t = s["text"].strip()
                if not t:
                    continue
                r = fitz.Rect(s["bbox"])
                if t.isdigit() and r.y0 > 470 and r.x0 > 600:   # numero di pagina
                    out.append((r, t, True, s))
                    continue
                f = s["font"]
                is_prose = line_prose or (s["size"] >= 19 and ("Comic" in f or "Cambria" in f or "Wingdings" in f))
                out.append((r, t, is_prose, s))
    return out


def figure_regions(page):
    pr = page.rect
    rects = []
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & pr
        if r.width >= 12 and r.height >= 12:
            rects.append(r)
    drs = [d for d in page.get_drawings()
           if not (d["rect"].width > FRAME_MIN and d["rect"].height > 500)]
    if drs:
        try:
            for r in page.cluster_drawings(drawings=drs, x_tolerance=6, y_tolerance=6):
                if r.width >= 25 and r.height >= 25:
                    rects.append(r & pr)
        except Exception:
            pass
    rects = merge(rects, 4)
    rects = [r for r in rects if r.width >= MIN_W and r.height >= MIN_H]
    if not rects:
        return []
    # etichette: span corti vicini o sovrapposti
    spans, prose = [], []
    for sr, t, is_prose, _ in text_spans(page):
        (prose if is_prose else spans).append((sr, t))
    for _ in range(2):
        for j in range(len(rects)):
            for sr, t in spans:
                r = rects[j]
                if sr in r:
                    continue
                inside = abs(sr & r) > 0.5 * abs(sr)
                if inside or (len(t) <= 8 and touch(r, sr, 10)):
                    rects[j] = r | sr
        rects = merge(rects, 2)
    return [fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2) & pr for r in rects]


def main():
    out = sys.argv[1]
    figdir = os.path.join(out, "fig")
    sheetdir = os.path.join(out, "sheets")
    os.makedirs(figdir, exist_ok=True)
    os.makedirs(sheetdir, exist_ok=True)
    index = {}
    if os.path.exists(os.path.join(out, "figure.json")):
        index = json.load(open(os.path.join(out, "figure.json")))
    ovp = os.path.join(out, "overrides_pt.json")
    if os.path.exists(ovp):
        OVERRIDES.update(json.load(open(ovp)))
    for path in sys.argv[2:]:
        deck = re.match(r"(\d+)", os.path.basename(path)).group(1)
        doc = fitz.open(path)
        regs = {}
        clean = fitz.open(path)
        for i, page in enumerate(doc):
            rs = figure_regions(page)
            # figure aggiuntive definite solo nei ritocchi manuali
            k = len(rs) + 1
            while "clip" in OVERRIDES.get(f"{deck}-{i+1:03d}-{k}", {}):
                rs.append(fitz.Rect(OVERRIDES[f"{deck}-{i+1:03d}-{k}"]["clip"]))
                k += 1
            cp = clean[i]
            if rs:
                relabel = []
                for sr, t, is_prose, sp in text_spans(page):
                    if not any(abs(sr & r) > 0 for r in rs):
                        continue
                    clips = [fitz.Rect(OVERRIDES[f"{deck}-{i+1:03d}-{k}"]["clip"])
                             for k in range(1, len(rs) + 1)
                             if "clip" in OVERRIDES.get(f"{deck}-{i+1:03d}-{k}", {})]
                    label_in_clip = len(t) <= 30 and any(sr in c for c in clips)
                    if is_prose and not label_in_clip:
                        cp.add_redact_annot(sr, fill=False)
                    elif "Comic" in sp["font"]:
                        # etichetta in Comic Sans: la si riscrive in Arial
                        cp.add_redact_annot(sr, fill=False)
                        relabel.append(sp)
                cp.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                    graphics=fitz.PDF_REDACT_LINE_ART_NONE)
                for sp in relabel:
                    bold = "Bold" in sp["font"]
                    name = "arialb" if bold else "arial"
                    cp.insert_font(fontname=name, fontfile=ARIAL_B if bold else ARIAL)
                    c = sp["color"]
                    cp.insert_text(sp["origin"], sp["text"], fontname=name, fontsize=sp["size"] * 0.92,
                                   color=((c >> 16) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255))
            for k, r in enumerate(rs, 1):
                fid = f"{deck}-{i+1:03d}-{k}"
                ov = OVERRIDES.get(fid, {})
                for m in ov.get("mask", []):
                    cp.draw_rect(fitz.Rect(m), color=None, fill=(1, 1, 1), overlay=True)
                if "clip" in ov:
                    r = fitz.Rect(ov["clip"])
                pix = cp.get_pixmap(clip=r, matrix=fitz.Matrix(FIG_ZOOM, FIG_ZOOM))
                pix.save(os.path.join(figdir, fid + ".png"))
                index[fid] = [round(v, 1) for v in r]
            regs[i] = rs
        # contact sheet 2x2 con riquadri
        W, H = 842 * 0.9, 595 * 0.9
        n = doc.page_count
        for start in range(0, n, 4):
            s = fitz.open()
            pg = s.new_page(width=W * 2 + 10, height=H * 2 + 10)
            for k, i in enumerate(range(start, min(start + 4, n))):
                x = (k % 2) * (W + 10)
                y = (k // 2) * (H + 10)
                box = fitz.Rect(x, y, x + W, y + H)
                pg.show_pdf_page(box, doc, i)
                sc = W / doc[i].rect.width
                for j, r in enumerate(regs[i], 1):
                    rr = fitz.Rect(x + r.x0 * sc, y + r.y0 * sc, x + r.x1 * sc, y + r.y1 * sc)
                    pg.draw_rect(rr, color=(1, 0, 0), width=1)
                    pg.insert_text((rr.x0 + 2, rr.y0 + 11), str(j), fontsize=11, color=(1, 0, 0))
                pg.insert_text((x + 4, y + H - 6), f"{deck}-{i+1}", fontsize=16, color=(0.8, 0, 0.8))
                pg.draw_rect(box, color=(0.6, 0.6, 0.6), width=0.5)
            pg.get_pixmap(dpi=72).save(os.path.join(sheetdir, f"{deck}-{start+1:03d}.png"))
        print(path, n, "slide,", sum(len(v) for v in regs.values()), "figure")
    with open(os.path.join(out, "figure.json"), "w") as f:
        json.dump(index, f, indent=0)


if __name__ == "__main__":
    main()
