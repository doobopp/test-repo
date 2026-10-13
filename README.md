# test-repo

## book_to_pdf.py: online book to a single PDF

Turns a multi-page online book built with Quarto or bookdown (default:
[Forecasting: Principles and Practice, the Pythonic Way](https://otexts.com/fpppy/))
into one print-style PDF for reading and annotating on a tablet.

What you get:

- Cover page (title, subtitle, authors, cover image when the site has one)
- Table of contents with dotted leaders and correct page numbers
- Part divider pages, "Chapter N" / "Appendix X" chapter openers
- All text, code (syntax highlighted, long lines wrapped rather than cut),
  code output, data-frame tables, figures, callouts, tabsets (every tab printed),
  footnotes and references
- Math typeset with MathJax using math fonts (sharp at any zoom, searchable)
- Clickable cross-references, figure/section links and footnotes inside the PDF
- PDF bookmarks (Part > Chapter > Section), running chapter headers, page numbers

### Run it

```bash
pip install -r requirements.txt
python -m playwright install chromium   # once
python book_to_pdf.py                   # writes fpppy.pdf
```

Options: `--page-size A4|Letter|A5|eink-10.3`, `--font-scale 1.2`, `--toc-depth 1|2|3`, `--out FILE`, `--exclude` (website-only
pages left out; default: translations, print-version, reviews, error),
`--max-pages N` (quick trial), `--no-google-fonts`, `--chromium PATH`.
E-ink tablets: `--page-size eink-10.3` sizes the page to a 10.3-inch 4:3 screen
(210 x 157.5 mm: Boox Note Air, reMarkable 2), so it is shown at 100% instead of
being shrunk to about 71% like A4, with 10% larger type. Use `--font-scale` to
go bigger or smaller.

Black-and-white e-ink: `--grayscale` makes all text black (code highlighting becomes
bold keywords and italic comments) and converts charts to grey so that series stay
distinguishable: each chart's series colours keep their light-to-dark order but get
distinct steps of a 16-level e-ink screen, instead of the plain luminance that can
give different colours the same grey (e.g. matplotlib's default blue and red).

File size: by default equations are drawn with fonts, charts are stored as
256-colour images when that is visually identical (checked per image, PSNR of at
least 45 dB), and the PDF is repacked losslessly. On a chart- and equation-heavy
test book this cut the file from 4.5 MB to 1.4 MB. `--math svg` and
`--no-image-optimization` restore the previous behaviour.

Downloads are cached in `.book_cache/`, so re-runs are fast. Node/npm is used, if
present, to install MathJax locally; otherwise it is loaded from jsDelivr.

The script checks its own output at the end: every table-of-contents entry must
be found on the page it claims. Keep the PDF for personal use and respect the
book's copyright.
