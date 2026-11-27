# pdf-to-epub-converter

Converte PDF scientifici in EPUB3 leggibili su e-reader (Apple Books, Kobo, Calibre…).
Funziona con libri, articoli a una o due colonne e slide, senza regolazioni per il singolo
documento: lo script misura il PDF prima di convertirlo.

## Uso

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# un libro o un articolo
.venv/bin/python scripts/pdf2epub.py libro.pdf libro.epub

# più PDF in un unico EPUB, un capitolo per PDF (es. le slide di un corso)
.venv/bin/python scripts/pdf2epub.py 1-intro.pdf 2-giunzione.pdf corso.epub --title "Corso" --authors "Nome Cognome"
```

Opzioni:

- `--title`, `--authors "A;B"`, `--publisher`, `--lang it|en|…`: metadati. Se non li indichi,
  vengono presi dal PDF o ricavati dalla prima pagina.
- `--pages 59-86`: converte solo un intervallo di pagine.
- `--slides` / `--pages-mode`: forzano la modalità, che di solito è rilevata da sola.
- `--report report.json`: salva il resoconto della conversione.

## Cosa rileva da solo

- **Corpo del testo e delle note.** Sono le dimensioni del carattere più frequenti, e regolano
  rientri, titoli e dimensioni delle immagini.
- **Testatine e piè di pagina.** Vengono tolti i numeri di pagina, le righe sulla stessa
  linea di base del numero di pagina e le righe ripetute nei margini.
- **Colonne.** Le pagine a due colonne si leggono colonna sinistra, poi colonna destra.
  Titolo, abstract, figure larghe e note su tutta la pagina restano fasce a sé.
- **Slide.** Le pagine orizzontali sono trattate come slide. Ogni casella di testo e ogni
  figura sono lette separatamente, e le slide con lo stesso titolo («(segue)», «cont.») sono
  unite in una sola sezione. Il Comic Sans dentro le figure viene ridisegnato in Arial.
- **Titoli.** Si usano i segnalibri del PDF quando ci sono. Altrimenti i titoli si
  riconoscono dal corpo maggiore o dalla numerazione in grassetto («2.1 Metodo»).
- **Note a piè di pagina.** Diventano note EPUB3 a comparsa (`noteref` più `aside`),
  agganciate al numerino nel testo. Sono gestite anche le note che proseguono alla pagina
  dopo e i richiami con simboli (*, †).
- **Formule.** Le formule in display e le tabelle diventano immagini nitide. Le formule nel
  testo che non sono scrivibili come testo (apice e pedice sovrapposti, radici, frecce,
  glifi senza Unicode) diventano piccole immagini allineate alla riga. Sono riconosciuti i
  font matematici di TeX, Word (Cambria Math), MathType, STIX e Symbol.

## Controlli nel resoconto

Lo script stampa, e salva con `--report`, un resoconto con:

- `math_glyphs_not_in_output`: i simboli matematici del PDF che non sono finiti né nel testo
  né in un'immagine. Dovrebbe essere 0 o quasi; in `pages_with_lost_math` ci sono le pagine
  da controllare.
- `footnotes`, `footnotes_without_reference`: le note trovate e quelle senza richiamo nel
  testo.
- `two_column_pages`, `running_heads`, `body_pt`, `note_pt`, `mode`: cosa ha rilevato
  l'analisi.

## Altri script

- `scripts/md2epub.py`: dispense scritte in Markdown leggero con LaTeX, convertite in EPUB
  con formule MathML (usa `tex2mathml.py`).
- `scripts/slides_extract.py`: ritaglio manuale delle figure dalle slide, con correzioni
  in `slides/overrides_pt.json`.
- `scripts/web2epub.py`: pagine web convertite in EPUB.
- `tests/make_twocol_sample.py`: genera un articolo sintetico a due colonne (note, formule,
  figura) per provare il convertitore.

## Limiti

- Le formule restano immagini, non MathML.
- I PDF scansionati (immagini senza testo) richiedono prima un passaggio di OCR.
- Con impaginazioni molto particolari conviene controllare il resoconto e le pagine che
  segnala.
