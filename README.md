# pdf-to-epub-converter

Turns scientific PDFs into EPUB3 books that read well on e-readers (Apple Books, Kobo,
Calibre…). It handles books, articles in one or two columns, and slide decks with no
per-document tuning: the script measures each PDF before converting it.

## Usage

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# a book or an article
.venv/bin/python scripts/pdf2epub.py book.pdf book.epub

# several PDFs in one EPUB, one chapter per PDF (e.g. the slides of a course)
.venv/bin/python scripts/pdf2epub.py 1-intro.pdf 2-junction.pdf course.epub --title "Course" --authors "First Last"
```

Options:

- `--title`, `--authors "A;B"`, `--publisher`, `--lang it|en|…`: metadata. When omitted,
  they are taken from the PDF or derived from its first page.
- `--pages 59-86`: convert only a page range.
- `--slides` / `--pages-mode`: force a mode. It is normally detected automatically.
- `--report report.json`: save the conversion report.

## What it detects on its own

- **Body and footnote size.** The most frequent font sizes. They drive indents, headings
  and image sizes.
- **Running heads and footers.** It removes page numbers, the lines that share the page
  number's baseline, and lines repeated in the margins.
- **Columns.** Two-column pages are read left column first, then right column. Title,
  abstract, wide figures and full-width footnotes stay bands of their own.
- **Slides.** Landscape pages are treated as slides. Each text box and each figure is read
  separately, and slides with the same title ("(cont.)", "(segue)") are merged into one
  section. PowerPoint equations split into many text boxes are put back together. Comic
  Sans inside figures is redrawn in Arial.
- **Headings.** It uses the PDF bookmarks when there are any. Otherwise headings are
  recognised by larger type or bold numbering ("2.1 Method").
- **Footnotes.** They become EPUB3 pop-up notes (`noteref` plus `aside`), attached to the
  reference mark in the text. Notes that continue on the next page and symbol marks
  (*, †) are handled.
- **Formulas.** Display formulas and tables become sharp images. Inline formulas that
  cannot be written as text (stacked sub/superscripts, roots, arrows, glyphs without
  Unicode) become small images aligned with the line. It recognises the math fonts of
  TeX, Word (Cambria Math), MathType, STIX and Symbol.

## Checks in the report

The script prints a report, and saves it with `--report`. It includes:

- `math_glyphs_not_in_output`: math symbols of the PDF that ended up neither in the text
  nor in an image. It should be 0 or close to it; `pages_with_lost_math` lists the pages
  to check.
- `footnotes`, `footnotes_without_reference`: the notes found and those with no
  reference mark in the text.
- `two_column_pages`, `running_heads`, `body_pt`, `note_pt`, `mode`: what the analysis
  detected.

## Other scripts

- `scripts/md2epub.py`: lecture notes written in light Markdown with LaTeX, converted to
  EPUB with MathML formulas (uses `tex2mathml.py`).
- `scripts/slides_extract.py`: manual figure cropping from slides, with corrections in
  `slides/overrides_pt.json`.
- `scripts/web2epub.py`: web pages converted to EPUB.
- `tests/make_twocol_sample.py`: generates a synthetic two-column article (footnotes,
  formulas, a figure) to test the converter.

## Limits

- Formulas stay images, not MathML.
- Scanned PDFs (images with no text) need an OCR pass first.
- For very unusual layouts, check the report and the pages it points to.
