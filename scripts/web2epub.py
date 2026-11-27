#!/usr/bin/env python3
"""Build an EPUB3 from a list of edutecnica.it theory pages.

Each page becomes a chapter: the content of <div class="main"> is cleaned,
its images are downloaded and embedded, and its headings feed the EPUB index.
The EPUB writer is shared with pdf2epub.py.

Usage:
    python web2epub.py OUTPUT.epub --title "..." URL[|Titolo capitolo] [...]
"""
import argparse
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import fitz  # PyMuPDF, only used to draw a simple cover
from bs4 import BeautifulSoup, Comment

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdf2epub import write_epub  # noqa: E402

UA = "Mozilla/5.0 (personal EPUB conversion)"
DELAY = 1.0            # seconds between requests, to be gentle with the site
PX_PER_EM = 16         # image sizes are expressed in em so they follow the text size
INLINE_MAX_H = 90      # images up to this height (px) inside text stay inline

KEEP_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "sub", "sup", "img", "ul", "ol", "li",
             "table", "thead", "tbody", "tr", "td", "th", "h1", "h2", "h3", "h4", "span",
             "div", "a", "hr", "pre", "code", "blockquote", "font", "center", "small"}
DROP_TAGS = {"script", "style", "iframe", "ins", "noscript", "form", "input", "button", "header",
             "nav", "video", "audio", "object", "embed"}


def fetch(url, cache_dir):
    """Download url once; later runs read it from the cache."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", urllib.parse.urlsplit(url).path.strip("/"))
    path = os.path.join(cache_dir, name)
    if not os.path.exists(path):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(path, "wb") as fh:
            fh.write(data)
        time.sleep(DELAY)
    with open(path, "rb") as fh:
        return fh.read()


def clean_main(soup):
    main = soup.find("div", class_="main") or soup.body
    for c in main.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    # interactive animations cannot work in an EPUB: leave a note in their place
    for t in main.find_all("canvas"):
        note = soup.new_tag("p")
        note.string = "[Animazione interattiva disponibile solo sul sito]"
        t.replace_with(note)
    for t in main.find_all(DROP_TAGS):
        t.decompose()
    for t in main.find_all(id="ads"):
        t.decompose()
    # unwrap presentational/unknown tags, keep their text
    for t in main.find_all(True):
        if t.name not in KEEP_TAGS or t.name in ("font", "center", "span", "small"):
            t.unwrap()
    return main


def convert_images(main, page_url, slug, cache_dir, images, report):
    for img in main.find_all("img"):
        src = img.get("src")
        if not src:
            img.decompose()
            continue
        url = urllib.parse.urljoin(page_url, src)
        try:
            data = fetch(url, cache_dir)
        except Exception as e:  # missing image: keep the text going
            report.append(f"immagine non scaricata: {url} ({e})")
            img.decompose()
            continue
        ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower() or ".png"
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in (".png", ".jpg", ".gif", ".svg"):
            report.append(f"formato immagine non supportato: {url}")
            img.decompose()
            continue
        name = f"{slug}_{len([k for k in images if k.startswith(slug + '_')]) + 1:03d}{ext}"
        images[name] = data
        w = _num(img.get("width"))
        h = _num(img.get("height"))
        if not w or not h:
            try:
                pix = fitz.Pixmap(data) if ext != ".svg" else None
                if pix:
                    w, h = pix.width, pix.height
            except Exception:
                pass
        alone = _alone_in_parent(img)
        attrs = {"src": f"img/{name}", "alt": img.get("alt", "")}
        if w:
            style = f"width:{w / PX_PER_EM:.2f}em;max-width:100%"
            if not alone and h and h <= INLINE_MAX_H:
                style += ";vertical-align:middle"
            attrs["style"] = style
        if alone or (h and h > INLINE_MAX_H):
            attrs["class"] = "block"
        img.attrs = attrs


def _num(v):
    try:
        return float(str(v).replace("px", ""))
    except (TypeError, ValueError):
        return None


def _alone_in_parent(img):
    text = "".join(s for s in img.parent.strings).strip().replace("\xa0", "")
    return not text


def tidy(main):
    # drop attributes we do not want (align, width, bgcolor, class, ...)
    for t in main.find_all(True):
        if t.name == "img":
            continue
        if t.name == "a":
            # links to other pages of the site are dead inside the EPUB
            t.unwrap()
            continue
        if t.name in ("td", "th"):
            t.attrs = {k: v for k, v in t.attrs.items() if k in ("colspan", "rowspan")}
        else:
            t.attrs = {}
    # remove empty paragraphs/divs (often &nbsp; spacers)
    changed = True
    while changed:
        changed = False
        for t in main.find_all(["p", "div", "li", "table"]):
            if not t.find("img") and not t.get_text().replace("\xa0", "").strip():
                t.decompose()
                changed = True
    # collapse runs of <br>
    for br in main.find_all("br"):
        nxt = br.next_sibling
        while nxt is not None and isinstance(nxt, str) and not nxt.strip():
            nxt = nxt.next_sibling
        if getattr(nxt, "name", None) == "br":
            br.decompose()


def page_blocks(main, page_title, base=1):
    """Split the page into heading blocks (for the index) and raw XHTML blocks.

    The page title gets level `base` and starts a new file; the page's own
    h1/h2/h3 go one, two and three levels below it.
    """
    blocks = [{"kind": "h", "level": base, "title": page_title, "split": True}]
    first_h1_skipped = False
    buf = []

    def flush():
        if buf:
            blocks.append({"kind": "raw", "html": "".join(buf)})
            buf.clear()

    for child in list(main.children):
        name = getattr(child, "name", None)
        if name in ("h1", "h2", "h3"):
            title = " ".join(child.get_text().split())
            if not title:
                continue
            if name == "h1" and not first_h1_skipped and title.lower() == page_title.lower():
                first_h1_skipped = True  # the page title is already the chapter heading
                continue
            flush()
            blocks.append({"kind": "h", "level": base + {"h1": 1, "h2": 2, "h3": 3}[name], "title": title})
        elif name is None:
            s = str(child).strip()
            if s:
                buf.append(f"<p>{_xml(s)}</p>")
        else:
            buf.append(child.decode(formatter="html5"))
    flush()
    for b in blocks:
        if b["kind"] == "raw":
            b["html"] = convert_jqmath(to_xhtml(b["html"]))
    return blocks


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_xhtml(fragment):
    """Re-serialise an HTML fragment as well-formed XHTML."""
    soup = BeautifulSoup(f"<div>{fragment}</div>", "lxml")
    div = soup.div
    out = "".join(c.decode(formatter="minimal") if hasattr(c, "decode") else _xml(str(c))
                  for c in div.contents)
    out = re.sub(r"<(br|hr|img)([^>]*?)\s*/?>", r"<\1\2/>", out)
    return out.replace("\xa0", "&#160;")


# ---------------------------------------------------------------- jqMath
# Some pages write formulas in jqMath ($...$ inline, $$...$$ display), drawn by a
# script in the browser. Convert the subset they use to plain HTML.
OVER = {"-": "\u0305", "—": "\u0305", "¯": "\u0305"}


def _group(s, i):
    """Return (content, next index) of a {...} group or of a single token at s[i]."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i < len(s) and s[i] == "{":
        depth, j = 1, i + 1
        while j < len(s) and depth:
            depth += {"{": 1, "}": -1}.get(s[j], 0)
            j += 1
        return s[i + 1:j - 1], j
    if i < len(s) and s[i] == "\\":
        m = re.match(r"\\[A-Za-z]+", s[i:])
        if m:
            return s[i:i + m.end()], i + m.end()
    return s[i:i + 1], i + 1


def jq_to_html(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c in "_^":
            g, i = _group(s, i + 1)
            tag = "sub" if c == "_" else "sup"
            out.append(f"<{tag}>{jq_to_html(g)}</{tag}>")
        elif c == "↖":  # accent over the previous symbol: x↖{→} is a vector
            g, i = _group(s, i + 1)
            g = g.strip()
            if out and not out[-1].startswith("<"):
                if g == "→":  # vectors in bold: combining arrows are missing from many fonts
                    out[-1] = f"<b>{out[-1]}</b>"
                else:
                    out[-1] = out[-1] + OVER.get(g, "\u0302")
        elif c == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", s[i:])
            name = m.group(1)
            i += m.end()
            if name == "ov":
                g, i = _group(s, i)
                out.append("".join(ch + "\u0305" for ch in re.sub(r"<[^>]+>", "", jq_to_html(g))))
            elif name in (";", " ", ",", ":"):
                out.append(" ")
            else:
                out.append(name)
        elif c in "{}":
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def convert_jqmath(fragment):
    fragment = re.sub(r"\$\$([^$<]+)\$\$", lambda m: f'<span class="jqd">{jq_to_html(m.group(1))}</span>', fragment)
    return re.sub(r"\$([^$<]+)\$", lambda m: f'<span class="jq">{jq_to_html(m.group(1))}</span>', fragment)


def make_cover(title, subtitle):
    doc = fitz.open()
    page = doc.new_page(width=600, height=900)
    page.draw_rect(page.rect, color=None, fill=(0.18, 0.55, 0.34))  # SeaGreen, like the site
    page.insert_textbox(fitz.Rect(50, 300, 550, 500), title, fontsize=46, fontname="hebo",
                        color=(1, 1, 1), align=1)
    page.insert_textbox(fitz.Rect(50, 520, 550, 640), subtitle, fontsize=22, fontname="helv",
                        color=(1, 1, 1), align=1)
    return page.get_pixmap().tobytes("png")


CSS_EXTRA = """
img.block { display: block; margin: 0.6em auto; }
table { border-collapse: collapse; margin: 0.8em auto; }
td, th { border: 1px solid #888; padding: 0.2em 0.4em; }
span.jq { font-style: italic; white-space: nowrap; }
span.jqd { display: block; text-align: center; font-style: italic; margin: 0.4em 0; }
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("epub")
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--list", help="file with one entry per line: 'URL|Titolo' or '=N Titolo' "
                                   "(a heading of level N; the pages after it go one level below)")
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default="edutecnica.it")
    ap.add_argument("--cache", default="web/cache")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)

    entries = list(a.urls)
    if a.list:
        with open(a.list, encoding="utf-8") as fh:
            entries += [l.strip() for l in fh if l.strip() and not l.lstrip().startswith("#")]
    blocks, images, report = [], {}, []
    base = 1
    for arg in entries:
        m = re.match(r"^=(\d)\s+(.+)$", arg)
        if m:  # part/group heading
            lvl = int(m.group(1))
            blocks.append({"kind": "h", "level": lvl, "title": m.group(2).strip(), "split": True})
            base = lvl + 1
            continue
        url, _, given_title = arg.partition("|")  # "URL|Titolo" sets the chapter title
        raw = fetch(url, a.cache)
        soup = BeautifulSoup(raw, "lxml", from_encoding=None)
        # folder + file name: two pages may share a file name (cm/campo.htm, campo/campo.htm)
        parts = os.path.splitext(urllib.parse.urlsplit(url).path)[0].strip("/").split("/")
        slug = "-".join(parts[-2:])
        h1 = soup.find("div", class_="main")
        h1 = h1.find("h1") if h1 else None
        title = given_title or (" ".join(h1.get_text().split()) if h1 else
                                (soup.title.get_text().strip() if soup.title else slug))
        main_div = clean_main(soup)
        convert_images(main_div, url, slug, a.cache, images, report)
        tidy(main_div)
        pb = page_blocks(main_div, title, base)
        blocks += pb
        n_img = len([k for k in images if k.startswith(slug + "_")])
        print(f"{title}: {sum(1 for b in pb if b['kind'] == 'h')} titoli, {n_img} immagini")

    import pdf2epub
    pdf2epub.CSS += CSS_EXTRA
    cover = make_cover(a.title, a.author)
    write_epub(blocks, images, cover, a.epub, {"title": a.title, "authors": [a.author],
                                                "publisher": "edutecnica.it", "date": time.strftime("%Y"),
                                                "nav_depth": 4})
    size = os.path.getsize(a.epub)
    print(f"\n{a.epub}: {sum(1 for e in entries if not e.startswith('='))} pagine, {len(images)} immagini, {size / 1e6:.1f} MB")
    for r in report:
        print("ATTENZIONE:", r)


if __name__ == "__main__":
    main()
