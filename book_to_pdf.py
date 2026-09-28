#!/usr/bin/env python3
"""Convert a multi-page online book (Quarto or bookdown) into a single print-style PDF.

Built for "Forecasting: Principles and Practice, the Pythonic Way"
(https://otexts.com/fpppy/), but works with most Quarto and bookdown books.

Pipeline
  1. Download the landing page and read the table of contents from its sidebar
     (parts, chapters, appendices, in reading order).
  2. Download every chapter page (raw HTML, cached on disk) and every image.
  3. Clean each page (drop navigation, buttons, sidebars), keep text, code,
     code output, tables, figures, callouts and footnotes, and rewrite every
     cross-page link so it points inside the single combined document.
  4. Build one HTML book: cover, table of contents, part pages, chapters.
     Math is typeset with MathJax (SVG output, so no web fonts are needed).
  5. Print it with headless Chromium twice: the first pass locates every
     heading on its page, the second fills in the table-of-contents page numbers.
  6. Post-process with PyMuPDF: running chapter headers, page numbers,
     a clickable PDF outline (bookmarks) and document metadata.

Usage
  pip install playwright beautifulsoup4 pymupdf
  python -m playwright install chromium      # skip if Chromium is already installed
  python book_to_pdf.py                      # writes fpppy.pdf
  python book_to_pdf.py --url https://otexts.com/fpppy/ --out fpppy.pdf --page-size A4

The downloaded pages are for personal use (reading and annotating your own copy);
respect the book's copyright and licence.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0 Safari/537.36 book-to-pdf/1.0 (personal offline copy)"
)
MATHJAX_CDN = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"
PLACEHOLDER_PAGE = "000"
TOP_MARGIN_MM, BOTTOM_MARGIN_MM, SIDE_MARGIN_MM = 22, 20, 18
TOP_MARGIN_PT, BOTTOM_MARGIN_PT = TOP_MARGIN_MM * 72 / 25.4, BOTTOM_MARGIN_MM * 72 / 25.4


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Page:
    url: str
    label: str  # text shown in the site's sidebar
    part: int | None  # index into Book.parts, None if outside any part
    slug: str = ""
    soup: Tag | None = None  # cleaned content
    title: str = ""  # chapter title as printed (e.g. "2 Time series graphics")
    ids: set[str] = field(default_factory=set)


@dataclass
class Part:
    title: str
    url: str | None  # some Quarto parts have their own intro page


@dataclass
class Book:
    base_url: str
    title: str = ""
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    cover_img: str | None = None
    parts: list[Part] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Downloading (with an on-disk cache so re-runs are fast and polite)
# --------------------------------------------------------------------------- #


class Fetcher:
    def __init__(self, cache_dir: Path, delay: float):
        self.cache_dir = cache_dir
        self.delay = delay
        (cache_dir / "pages").mkdir(parents=True, exist_ok=True)
        (cache_dir / "assets").mkdir(parents=True, exist_ok=True)
        self._asset_map: dict[str, str] = {}

    def _get(self, url: str) -> bytes:
        last_err: Exception | None = None
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                time.sleep(self.delay)
                return data
            except urllib.error.HTTPError as e:
                if e.code in (403, 404, 410):
                    raise
                last_err = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
            time.sleep(2 ** (attempt + 1))
        raise RuntimeError(f"Failed to download {url}: {last_err}")

    def page(self, url: str) -> str:
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        path = self.cache_dir / "pages" / f"{key}.html"
        if not path.exists():
            print(f"  downloading {url}")
            path.write_bytes(self._get(url))
        return path.read_bytes().decode("utf-8", errors="replace")

    def asset(self, url: str) -> str | None:
        """Download an image; return its local path relative to the cache dir."""
        if url in self._asset_map:
            return self._asset_map[url]
        name = Path(urlparse(url).path).name or "asset"
        ext = Path(name).suffix.lower()[:6] or ".bin"
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        rel = f"assets/{key}{ext}"
        path = self.cache_dir / rel
        if not path.exists():
            try:
                path.write_bytes(self._get(url))
            except Exception as e:  # keep going: a missing image should not kill the book
                print(f"  WARNING: could not download image {url}: {e}")
                self._asset_map[url] = None
                return None
        self._asset_map[url] = rel
        return rel


# --------------------------------------------------------------------------- #
# Table of contents discovery
# --------------------------------------------------------------------------- #


def norm_url(url: str) -> str:
    """Canonical form used to recognise internal pages."""
    url, _ = urldefrag(url)
    if url.endswith("/"):
        url += "index.html"
    return url


def is_internal(url: str, base: str) -> bool:
    u, b = urlparse(url), urlparse(base)
    base_dir = b.path if b.path.endswith("/") else b.path.rsplit("/", 1)[0] + "/"
    return u.netloc == b.netloc and u.path.startswith(base_dir) and u.path.endswith(".html")


def clean_text(el: Tag) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def discover_structure(index_html: str, base_url: str) -> Book:
    soup = BeautifulSoup(index_html, "html.parser")
    book = Book(base_url=base_url)
    index_url = norm_url(base_url)

    seen: set[str] = set()

    def add_page(href: str, label: str, part: int | None):
        if not href or href.startswith(("javascript:", "mailto:")) or href.startswith("#"):
            return
        url = norm_url(urljoin(base_url, href))
        if not is_internal(url, base_url) or url in seen:
            return
        seen.add(url)
        book.pages.append(Page(url=url, label=label, part=part))

    sidebar = soup.select_one("#quarto-sidebar")
    summary = soup.select_one(".book-summary ul.summary, nav#toc ul, #book-toc ul")

    if sidebar is not None:  # ---- Quarto book
        top = sidebar.select_one("ul")

        def walk(ul: Tag, part: int | None):
            for li in ul.find_all("li", recursive=False):
                classes = li.get("class", [])
                container = li.find(class_="sidebar-item-container", recursive=False) or li
                link = container.find("a")
                label = clean_text(container.select_one(".menu-text") or container)
                sub = li.find("ul", recursive=False)
                if "sidebar-item-section" in classes and sub is not None:
                    new_part = part
                    if part is None:  # top-level section = book part
                        book.parts.append(Part(title=label, url=None))
                        new_part = len(book.parts) - 1
                    href = link.get("href") if link else None
                    if href and href.endswith(".html"):
                        if part is None:
                            book.parts[new_part].url = norm_url(urljoin(base_url, href))
                        add_page(href, label, new_part)
                    walk(sub, new_part)
                elif link is not None:
                    add_page(link.get("href"), label, part)

        if top is not None:
            walk(top, None)
    elif summary is not None:  # ---- bookdown (gitbook / bs4_book)
        part: int | None = None
        for li in summary.find_all("li"):
            classes = li.get("class", [])
            if ("part" in classes or "appendix" in classes) and li.find("a") is None:
                book.parts.append(Part(title=clean_text(li), url=None))
                part = len(book.parts) - 1
                continue
            a = li.find("a")
            if a is not None and (li.find_parent("li") is None or "chapter" in classes):
                add_page(a.get("href"), clean_text(a), part)
    else:  # ---- generic fallback: every internal link in navigation elements
        for a in soup.select("nav a[href], aside a[href], .toc a[href]"):
            add_page(a.get("href"), clean_text(a), None)

    # Make sure the landing page (preface) comes first.
    if index_url not in seen:
        book.pages.insert(0, Page(url=index_url, label="Preface", part=None))
    else:
        book.pages.sort(key=lambda p: p.url != index_url)

    # Book metadata
    def meta(name: str) -> list[str]:
        return [m.get("content", "").strip() for m in soup.select(f'meta[name="{name}"]') if m.get("content")]

    title_el = soup.select_one("#title-block-header h1.title, h1.title")
    book.title = (meta("citation_title") or [clean_text(title_el) if title_el else ""])[0]
    if not book.title and soup.title:
        book.title = clean_text(soup.title).split(" - ")[0]
    sub_el = soup.select_one("#title-block-header .subtitle, p.subtitle")
    book.subtitle = clean_text(sub_el) if sub_el else ""
    authors = meta("citation_author") or meta("author")
    if not authors:
        authors = [clean_text(p) for p in soup.select(".quarto-title-meta-contents p.author, .quarto-title-author-name, p.author")]
    book.authors = [a for a in dict.fromkeys(authors) if a]
    cover = soup.select_one("img.quarto-cover-image, .cover img, img.cover")
    if cover is not None and cover.get("src"):
        book.cover_img = urljoin(base_url, cover["src"])
    return book


# --------------------------------------------------------------------------- #
# Page cleaning
# --------------------------------------------------------------------------- #

REMOVE_SELECTORS = [
    "script", "noscript", "link", "style", "button", "form", "input",
    "nav.page-navigation", ".page-navigation", "#quarto-margin-sidebar", "#quarto-sidebar",
    "#quarto-header", ".quarto-alternate-formats", ".code-copy-button", ".code-tools",
    ".code-with-copy > .code-copy-outer-scaffold > button", ".quarto-title-meta",
    ".quarto-title-banner .quarto-title-meta", ".quarto-categories", "#quarto-search",
    ".anchorjs-link", "a.anchor-section", ".book-header", ".navigation", ".page-nav",
    ".toc-actions", "#TOC", "nav#TOC", ".sidebar", ".quarto-other-links", ".quarto-code-links",
    ".nav-footer", "footer.footer", "#quarto-back-to-top", ".copy-to-clipboard-button",
]


def main_content(soup: BeautifulSoup) -> Tag:
    for sel in (
        "main#quarto-document-content",
        "main.content",
        ".page-inner section.normal",
        "main#content",
        "div#main-content",
        "main",
        "article",
        "body",
    ):
        el = soup.select_one(sel)
        if el is not None:
            return el
    return soup


def slugify(url: str, base: str) -> str:
    path = urlparse(url).path
    base_path = urlparse(base).path
    rel = path[len(base_path):] if path.startswith(base_path) else Path(path).name
    rel = re.sub(r"\.html$", "", rel) or "index"
    return "p-" + re.sub(r"[^A-Za-z0-9_-]+", "-", rel).strip("-")


def prepare_page(page: Page, raw_html: str, book: Book, fetcher: Fetcher, is_index: bool):
    soup = BeautifulSoup(raw_html, "html.parser")
    content = main_content(soup)
    for sel in REMOVE_SELECTORS:
        for el in content.select(sel):
            el.decompose()

    # The landing page's title block repeats the book title: it lives on the cover.
    if is_index:
        for el in content.select("#title-block-header, header#title-block-header, .quarto-title-block"):
            el.decompose()
        for el in content.select("img.quarto-cover-image, .cover"):
            el.decompose()

    # Tabsets: print every tab, each preceded by its label.
    for tabset in content.select(".panel-tabset"):
        labels = [clean_text(a) for a in tabset.select(".nav-tabs .nav-link, .nav-tabs a")]
        nav = tabset.select_one(".nav-tabs, ul.nav")
        if nav is not None:
            nav.decompose()
        for i, pane in enumerate(tabset.select(".tab-pane")):
            if i < len(labels):
                lab = soup.new_tag("p", attrs={"class": "tab-label"})
                lab.string = labels[i]
                pane.insert(0, lab)

    # Collapsed code / details: show them expanded.
    for d in content.find_all("details"):
        d["open"] = ""

    # Embedded media cannot be printed: replace with a link.
    for el in content.find_all(["iframe", "video", "audio"]):
        source = el.find("source")
        src = el.get("src") or (source.get("src") if source is not None else None)
        p = soup.new_tag("p", attrs={"class": "media-link"})
        if src:
            a = soup.new_tag("a", href=urljoin(page.url, src))
            a.string = f"[Embedded media: {urljoin(page.url, src)}]"
            p.append(a)
        else:
            p.string = "[Embedded media]"
        el.replace_with(p)

    # Images: download and reference locally.
    for img in content.find_all("img"):
        src = img.get("src") or img.get("data-src")
        for attr in ("srcset", "loading", "data-src", "sizes"):
            img.attrs.pop(attr, None)
        if not src or src.startswith("data:"):
            continue
        local = fetcher.asset(urljoin(page.url, src))
        if local:
            img["src"] = local
        else:
            img["src"] = urljoin(page.url, src)

    # Ensure the chapter starts with an h1.
    h1 = content.find("h1")
    if h1 is None:
        h1 = soup.new_tag("h1", attrs={"class": "title"})
        h1.string = page.label
        content.insert(0, h1)
    page.title = clean_text(h1) or page.label

    # Prefix every id so ids from different pages cannot collide (skip SVG internals).
    page.slug = slugify(page.url, book.base_url)
    for el in content.find_all(id=True):
        if el.find_parent("svg") is not None:
            continue
        page.ids.add(el["id"])
        el["id"] = f"{page.slug}--{el['id']}"

    content.name = "div"
    content.attrs = {"class": "chapter-body"}
    page.soup = content


def rewrite_links(book: Book):
    by_url = {p.url: p for p in book.pages}
    part_url = {pt.url: i for i, pt in enumerate(book.parts) if pt.url}
    for page in book.pages:
        for a in page.soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("mailto:", "javascript:", "data:")):
                continue
            target = urljoin(page.url, href)
            url, frag = urldefrag(target)
            url = norm_url(url) if url else page.url
            dest = by_url.get(url)
            if dest is not None:
                if frag and frag in dest.ids:
                    a["href"] = f"#{dest.slug}--{frag}"
                else:
                    a["href"] = f"#{dest.slug}"
                a.attrs.pop("target", None)
            elif url in part_url:
                a["href"] = f"#part-{part_url[url]}"
            else:
                a["href"] = target  # external link, made absolute


# --------------------------------------------------------------------------- #
# Assembling the single HTML document
# --------------------------------------------------------------------------- #

CSS = r"""
@page { size: %(page_size)s; margin: %(mt)smm %(ms)smm %(mb)smm %(ms)smm; }
:root {
  --ink: #1d1d1f; --muted: #5f6368; --rule: #d0d4d9; --accent: #1f4e79;
  --code-bg: #f6f8fa; --out-bg: #fbfbfb;
}
html { font-size: 10.5pt; }
body {
  margin: 0; color: var(--ink); background: #fff;
  font-family: "Source Serif 4", "Source Serif Pro", Charter, "Bitstream Charter",
               Georgia, "DejaVu Serif", "Liberation Serif", serif;
  line-height: 1.45; text-align: justify; hyphens: auto;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1, h2, h3, h4, h5, h6, .part-page, .cover, .toc, caption, figcaption, .callout-header,
.tab-label, th {
  font-family: "Source Sans 3", "Source Sans Pro", "Helvetica Neue", Arial, "DejaVu Sans", sans-serif;
  text-align: left; hyphens: manual;
}
h1, h2, h3, h4 { color: var(--accent); break-after: avoid; page-break-after: avoid; line-height: 1.2; position: relative; }
h1 { font-size: 22pt; margin: 0 0 1.2em; padding-bottom: .35em; border-bottom: 1.5pt solid var(--accent); }
h1 .chapter-number { display: block; font-size: 13pt; color: var(--muted); font-weight: 600;
  letter-spacing: .08em; text-transform: uppercase; margin-bottom: .3em; }
h1 .chapter-number::before { content: "Chapter "; }
.appendix-chapter h1 .chapter-number::before { content: "Appendix "; }
h2 { font-size: 15pt; margin: 1.6em 0 .6em; }
h3 { font-size: 12.5pt; margin: 1.3em 0 .5em; }
h4, h5, h6 { font-size: 11pt; margin: 1.1em 0 .4em; }
.header-section-number { color: var(--muted); margin-right: .45em; }
p { margin: 0 0 .7em; orphans: 3; widows: 3; }
a { color: var(--accent); text-decoration: none; }
ul, ol { padding-left: 1.6em; }
li { margin: .15em 0; }
blockquote { margin: 1em 0; padding: .2em 1em; border-left: 3pt solid var(--rule); color: #333; }
hr { border: 0; border-top: .6pt solid var(--rule); margin: 1.5em 0; }

/* --- structure --- */
.chapter { break-before: page; page-break-before: always; }
.part-page { break-before: page; break-after: page; height: 230mm; display: flex;
  flex-direction: column; justify-content: center; align-items: center; text-align: center; }
.part-page .part-label { font-size: 13pt; letter-spacing: .25em; text-transform: uppercase; color: var(--muted); }
.part-page h1 { border: 0; font-size: 28pt; margin: .4em 0 0; text-align: center; }
.part-intro { break-before: auto; }

/* --- cover --- */
.cover { height: 240mm; display: flex; flex-direction: column; justify-content: center;
  align-items: center; text-align: center; break-after: page; }
.cover .cover-title { font-size: 30pt; font-weight: 700; color: var(--accent); line-height: 1.15; margin: 0 0 .3em; }
.cover .cover-subtitle { font-size: 15pt; color: var(--muted); margin: 0 0 1.4em; }
.cover .cover-authors { font-size: 14pt; margin: 1em 0 2em; }
.cover img { max-height: 120mm; max-width: 80%%; margin: 1em 0; box-shadow: 0 1mm 4mm rgba(0,0,0,.15); }
.cover .cover-source { font-size: 9pt; color: var(--muted); margin-top: 2em; }

/* --- table of contents --- */
.toc { break-after: page; }
.toc h1 { margin-bottom: .8em; }
.toc ol { list-style: none; padding: 0; margin: 0; }
.toc li { margin: 0; }
.toc a { color: var(--ink); display: flex; align-items: baseline; gap: .4em; }
.toc .t { flex: 0 1 auto; }
.toc .dots { flex: 1 1 auto; border-bottom: .8pt dotted #9aa0a6; transform: translateY(-.25em); min-width: 1em; }
.toc .n { flex: 0 0 2.6em; text-align: right; font-variant-numeric: tabular-nums; }
.toc .lvl-part { font-weight: 700; font-size: 11.5pt; text-transform: uppercase; letter-spacing: .06em;
  margin-top: 1.1em; color: var(--accent); }
.toc .lvl-part a { color: var(--accent); }
.toc .lvl-ch { font-weight: 600; margin-top: .55em; font-size: 10.5pt; }
.toc .lvl-sec { font-size: 9.5pt; padding-left: 1.6em; color: #333; }
.toc .lvl-sec a { color: #333; }

/* --- code --- */
pre, code, kbd, samp { font-family: "JetBrains Mono", "Source Code Pro", "DejaVu Sans Mono", Menlo, Consolas, monospace; }
code { font-size: .86em; background: var(--code-bg); padding: .05em .25em; border-radius: 2pt; }
pre { font-size: 8.3pt; line-height: 1.38; background: var(--code-bg); border: .6pt solid #e1e4e8;
  border-radius: 3pt; padding: 6pt 8pt; margin: .6em 0 .9em; white-space: pre-wrap;
  overflow-wrap: anywhere; word-break: normal; text-align: left; hyphens: none; }
pre code { background: none; padding: 0; font-size: inherit; border: 0; white-space: inherit; }
div.sourceCode { margin: 0; background: none; border: 0; overflow: visible; }
.cell { margin: .8em 0; }
.cell-output pre, .cell-output-stdout pre, .cell-output-stderr pre, pre.output, .output pre {
  background: var(--out-bg); border: 0; border-left: 2.5pt solid #c8ccd0; border-radius: 0; color: #333; }
.cell-output-stderr pre { border-left-color: #e0b0a8; }
.cell-output-display { margin: .5em 0; }
pre > code.sourceCode > span { display: inline; }
pre.numberSource code > span > a:first-child::before { display: none; }
/* Pandoc / Quarto syntax highlighting (GitHub-like palette) */
code span.kw, code span.cf, code span.im { color: #cf222e; }
code span.dt, code span.bu { color: #8250df; }
code span.fu { color: #6639ba; }
code span.dv, code span.bn, code span.fl, code span.cn, code span.sc { color: #0550ae; }
code span.st, code span.ch, code span.vs, code span.ss { color: #0a3069; }
code span.co, code span.do, code span.an, code span.cv, code span.in { color: #6e7781; font-style: italic; }
code span.op { color: #24292f; }
code span.va, code span.at { color: #953800; }
code span.ot, code span.pp { color: #116329; }
code span.al, code span.er, code span.wa { color: #cf222e; font-weight: bold; }
code span.ex { color: #8250df; }

/* --- figures & tables --- */
img, svg.figure, figure svg { max-width: 100%%; height: auto; }
figure, .quarto-figure, .figure { margin: 1em 0; text-align: center; break-inside: avoid; page-break-inside: avoid; }
figure img, .quarto-figure img, .cell-output-display img { max-height: 190mm; object-fit: contain; }
figcaption, .caption, caption, .figure-caption, .table-caption {
  font-size: 9pt; color: var(--muted); text-align: left; margin: .4em 0 .2em; line-height: 1.35; caption-side: top; }
.quarto-layout-row { display: flex; gap: 4mm; justify-content: center; }
.quarto-layout-cell { flex: 1 1 0; min-width: 0; }
.quarto-float-caption-top { margin-bottom: .4em; }
table { border-collapse: collapse; margin: .8em auto 1.1em; font-size: 8.8pt; line-height: 1.3;
  max-width: 100%%; text-align: left; hyphens: none; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th, td { padding: 2.5pt 6pt; border-bottom: .5pt solid var(--rule); vertical-align: top; overflow-wrap: anywhere; }
thead th { border-bottom: 1pt solid #888; font-weight: 600; }
table.dataframe, .cell-output-display table { font-size: 8pt; font-family: "Source Sans 3", "DejaVu Sans", sans-serif; }
table.dataframe th, table.dataframe td { padding: 2pt 5pt; white-space: nowrap; }

/* --- callouts --- */
.callout { border: .6pt solid var(--rule); border-left: 4pt solid #0d6efd; border-radius: 3pt;
  margin: 1em 0; padding: 0; break-inside: avoid; page-break-inside: avoid; background: #fbfcfe; }
.callout-header { font-weight: 700; font-size: 10pt; padding: 4pt 9pt; background: rgba(13,110,253,.07); display: flex; gap: .4em; }
.callout-icon-container, .callout-toggle { display: none; }
.callout-body, .callout-body-container, .callout > :not(.callout-header) { padding: 5pt 9pt 2pt; }
.callout-body-container .callout-body { padding: 0; }
.callout.callout-tip { border-left-color: #198754; } .callout-tip .callout-header { background: rgba(25,135,84,.08); }
.callout.callout-warning { border-left-color: #fd7e14; } .callout-warning .callout-header { background: rgba(253,126,20,.08); }
.callout.callout-important { border-left-color: #dc3545; } .callout-important .callout-header { background: rgba(220,53,69,.08); }
.callout.callout-caution { border-left-color: #ffc107; } .callout-caution .callout-header { background: rgba(255,193,7,.1); }
.collapse:not(.show) { display: block !important; }

/* --- tabsets, footnotes, misc --- */
.tab-content > .tab-pane { display: block !important; opacity: 1 !important; }
.tab-label { font-weight: 700; font-size: 9.5pt; color: var(--muted); margin: .8em 0 .2em; text-transform: uppercase; letter-spacing: .05em; }
.footnotes, section.footnotes { font-size: 8.8pt; border-top: .6pt solid var(--rule); margin-top: 2em; padding-top: .5em; }
.footnotes hr { display: none; }
.footnote-ref, a[role="doc-noteref"] { font-size: .75em; vertical-align: super; line-height: 0; }
.hidden, .visually-hidden, .d-none, .sr-only { display: none !important; }
.column-margin, .margin-aside, aside { font-size: 9pt; color: var(--muted); border-left: 2pt solid var(--rule); padding-left: 8pt; margin: .8em 0; }
mjx-container[display="true"] { margin: .8em 0 !important; overflow: visible; }
mjx-container svg { max-width: 100%%; }
.media-link { font-style: italic; color: var(--muted); }
.csl-entry { margin-bottom: .5em; padding-left: 1.5em; text-indent: -1.5em; text-align: left; }
.pm { float: left; width: 0; height: 0; overflow: visible; font-size: 2pt; line-height: 0; color: #000; white-space: nowrap; }
"""


def e(text: str) -> str:
    return html.escape(text, quote=True)


@dataclass
class Heading:
    marker: str
    level: int  # outline level (1 = top)
    text: str
    anchor: str
    in_toc: str | None  # css class if shown in the printed TOC


def assemble_html(book: Book, mathjax_src: str, with_markers: bool, page_numbers: dict[str, int],
                  page_size: str, toc_depth: int, google_fonts: bool) -> tuple[str, list[Heading], list[tuple[str, str]]]:
    """Return (html, headings, chapter_markers)."""
    headings: list[Heading] = []
    chapters: list[tuple[str, str]] = []  # (marker, running-header text)
    counter = [0]

    def marker(prefix: str) -> str:
        counter[0] += 1
        return f"@@{prefix}{counter[0]:05d}@@"

    def mark_html(m: str) -> str:
        return f'<span class="pm">{m}</span>' if with_markers else ""

    body: list[str] = []
    # Cover
    authors = ", ".join(book.authors[:-1]) + (" and " if len(book.authors) > 1 else "") + (book.authors[-1] if book.authors else "")
    cover_img = f'<img src="{e(book.cover_img)}" alt="">' if book.cover_img else ""
    body.append(
        f'<section class="cover">{cover_img}<div class="cover-title">{e(book.title)}</div>'
        + (f'<div class="cover-subtitle">{e(book.subtitle)}</div>' if book.subtitle else "")
        + (f'<div class="cover-authors">{e(authors)}</div>' if authors else "")
        + f'<div class="cover-source">Offline copy of {e(book.base_url)} &middot; generated {time.strftime("%Y-%m-%d")}</div></section>'
    )

    # Build the chapter sections first (to know headings), then the TOC.
    toc_items: list[Heading] = []
    chapter_html: list[str] = []
    current_part: int | None = None
    appendix_mode = False
    for page in book.pages:
        if page.part is not None and page.part != current_part:
            current_part = page.part
            part = book.parts[page.part]
            appendix_mode = bool(re.match(r"appendi", part.title, re.I))
            m = marker("P")
            h = Heading(m, 1, part.title, f"part-{page.part}", "lvl-part")
            headings.append(h)
            toc_items.append(h)
            chapters.append((m, ""))
            chapter_html.append(
                f'<section class="part-page" id="part-{page.part}">'
                f'<div class="part-label">{"" if appendix_mode else "Part"}</div>'
                f'<h1>{mark_html(m)}{e(part.title)}</h1></section>'
            )
        elif page.part is None:
            current_part = None
            appendix_mode = False
        base_level = 2 if page.part is not None else 1
        is_part_intro = page.part is not None and book.parts[page.part].url == page.url
        # Headings inside the chapter
        first_h1 = True
        for hx in page.soup.find_all(re.compile(r"^h[1-4]$")):
            lvl = int(hx.name[1])
            if hx.find_parent(class_=re.compile(r"callout|footnotes")):
                continue
            text = clean_text(hx)
            if not text:
                continue
            if lvl == 1 and first_h1:
                first_h1 = False
                if is_part_intro:  # already represented by the part page
                    continue
                anchor = page.slug
                m = marker("C")
                chapters.append((m, text))
                cls = "lvl-ch" if toc_depth >= 1 else None
            else:
                if not hx.get("id"):
                    hx["id"] = f"{page.slug}--h{counter[0] + 1}"
                anchor = hx["id"]
                m = marker("H")
                cls = "lvl-sec" if (lvl == 2 and toc_depth >= 2) or (lvl == 3 and toc_depth >= 3) else None
            outline_level = base_level + (lvl - 1)
            h = Heading(m, outline_level, text, anchor, cls)
            headings.append(h)
            if cls:
                toc_items.append(h)
            if with_markers:
                pm = BeautifulSoup(mark_html(m), "html.parser")
                hx.insert(0, pm)
        extra_cls = " appendix-chapter" if appendix_mode else ""
        extra_cls += " part-intro" if is_part_intro else ""
        chapter_html.append(f'<section class="chapter{extra_cls}" id="{page.slug}">{page.soup.decode_contents()}</section>')
        if with_markers:  # remove markers from the soup so later passes start clean
            for pm in page.soup.select("span.pm"):
                pm.decompose()

    # Table of contents
    toc = ['<section class="toc"><h1>Contents</h1><ol>']
    for h in toc_items:
        n = page_numbers.get(h.marker, PLACEHOLDER_PAGE)
        toc.append(
            f'<li class="{h.in_toc}"><a href="#{e(h.anchor)}"><span class="t">{e(h.text)}</span>'
            f'<span class="dots"></span><span class="n">{n}</span></a></li>'
        )
    toc.append("</ol></section>")
    body.append("".join(toc))
    body.extend(chapter_html)

    fonts = (
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700'
        '&family=Source+Serif+4:ital,wght@0,400;0,600;0,700;1,400&family=JetBrains+Mono:wght@400;700&display=block">'
        if google_fonts else ""
    )
    mathjax_cfg = {
        "tex": {
            "inlineMath": [["\\(", "\\)"]],
            "displayMath": [["\\[", "\\]"], ["$$", "$$"]],
            "processEscapes": True,
            "packages": {"[+]": ["ams", "newcommand", "boldsymbol"]},
        },
        "svg": {"fontCache": "global"},
        "options": {"skipHtmlTags": ["script", "noscript", "style", "textarea", "pre", "code"]},
        "startup": {"typeset": True},
    }
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{e(book.title)}</title>
{fonts}
<style>{CSS % {"page_size": page_size, "mt": TOP_MARGIN_MM, "mb": BOTTOM_MARGIN_MM, "ms": SIDE_MARGIN_MM}}</style>
<script>window.MathJax = {json.dumps(mathjax_cfg)};</script>
<script id="MathJax-script" src="{e(mathjax_src)}"></script>
</head><body>
{''.join(body)}
</body></html>"""
    return doc, headings, chapters


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def find_mathjax(cache_dir: Path, explicit: str | None) -> str:
    if explicit:
        return Path(explicit).resolve().as_uri() if Path(explicit).exists() else explicit
    local = cache_dir / "mathjax" / "node_modules" / "mathjax" / "es5" / "tex-svg.js"
    if not local.exists() and shutil.which("npm"):
        print("Installing MathJax locally (npm) ...")
        (cache_dir / "mathjax").mkdir(exist_ok=True)
        subprocess.run(["npm", "install", "--silent", "--prefix", str(cache_dir / "mathjax"), "mathjax@3"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if local.exists():
        return local.resolve().as_uri()
    return MATHJAX_CDN


def render_pdf(html_path: Path, pdf_path: Path, page_size: str, chromium_path: str | None):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        kwargs = {"executable_path": chromium_path} if chromium_path else {}
        browser = p.chromium.launch(**kwargs)
        page = browser.new_page()
        page.set_default_timeout(15 * 60 * 1000)
        page.goto(html_path.resolve().as_uri(), wait_until="load")
        page.wait_for_function("() => window.MathJax && MathJax.startup && MathJax.startup.promise")
        page.evaluate("() => MathJax.startup.promise")
        page.evaluate("() => document.fonts.ready")
        page.wait_for_function("() => Array.from(document.images).every(i => i.complete)")
        errors = page.evaluate("() => document.querySelectorAll('mjx-merror, [data-mjx-error]').length")
        if errors:
            print(f"  WARNING: MathJax reported {errors} formula(s) it could not typeset")
        page.pdf(path=str(pdf_path), format=page_size, print_background=True, prefer_css_page_size=True,
                 display_header_footer=False)
        browser.close()


def locate_markers(pdf_path: Path) -> dict[str, tuple[int, float]]:
    """Return marker -> (0-based page index, y position in points)."""
    import pymupdf as fitz

    # When a heading starts a new page, Chromium also paints its marker at the
    # bottom of the previous page (sometimes split into pieces), so the last
    # page on which a marker appears is the one that holds the heading.
    # The copy on the right page may be clipped into fragments, while the stray
    # copy lands in the bottom margin band, where no real heading can start.
    # So a marker found in that band means "top of the next page".
    found: dict[str, tuple[int, float]] = {}
    with fitz.open(pdf_path) as doc:
        for pno, page in enumerate(doc):
            bottom_band = page.rect.height - BOTTOM_MARGIN_PT - 12
            for m in dict.fromkeys(re.findall(r"@@[PCH]\d{5}@@", re.sub(r"\s+", "", page.get_text()))):
                hits = page.search_for(m)
                y = min(r.y0 for r in hits) if hits else 0.0
                if hits and y >= bottom_band and pno + 1 < len(doc):
                    found[m] = (pno + 1, TOP_MARGIN_PT)
                else:
                    found[m] = (pno, y)
    return found


def postprocess(pdf_in: Path, pdf_out: Path, book: Book, headings: list[Heading],
                chapters: list[tuple[str, str]], positions: dict[str, tuple[int, float]]):
    import pymupdf as fitz

    doc = fitz.open(pdf_in)
    n = len(doc)
    # Running header: the chapter in effect on each page.
    starts = sorted((positions[m][0], text) for m, text in chapters if m in positions)
    start_pages = {p for p, _ in starts}
    running = [""] * n
    cur = ""
    it = iter(starts + [(n, "")])
    nxt = next(it)
    for i in range(n):
        while nxt[0] <= i and nxt[0] < n:
            cur = nxt[1]
            nxt = next(it)
        running[i] = cur

    grey = (0.42, 0.44, 0.47)
    for i, page in enumerate(doc):
        if i == 0:
            continue  # cover
        w, h = page.rect.width, page.rect.height
        num = str(i + 1)
        tw = fitz.get_text_length(num, fontname="helv", fontsize=9)
        page.insert_text(((w - tw) / 2, h - 28), num, fontname="helv", fontsize=9, color=grey)
        text = running[i]
        if text and i not in start_pages:
            text = text.encode("latin-1", "replace").decode("latin-1")
            max_w = w - 2 * 51
            while fitz.get_text_length(text + "...", fontname="helv", fontsize=8.5) > max_w and len(text) > 4:
                text = text[:-1].rstrip()
                if fitz.get_text_length(text + "...", fontname="helv", fontsize=8.5) <= max_w:
                    text += "..."
                    break
            title = book.title.encode("latin-1", "replace").decode("latin-1")
            if fitz.get_text_length(title, fontname="helv", fontsize=8.5) + fitz.get_text_length(text, fontname="helv", fontsize=8.5) > max_w - 20:
                title = ""
            tw = fitz.get_text_length(text, fontname="helv", fontsize=8.5)
            page.insert_text((w - 51 - tw, 40), text, fontname="helv", fontsize=8.5, color=grey)
            if title:
                page.insert_text((51, 40), title, fontname="helv", fontsize=8.5, color=grey)
            page.draw_line((51, 45), (w - 51, 45), color=(0.8, 0.82, 0.85), width=0.5)

    # PDF outline (bookmarks) with exact vertical positions.
    toc = [[1, "Contents", 2]]
    prev = 1
    for hd in headings:
        if hd.marker not in positions:
            continue
        pno, y = positions[hd.marker]
        lvl = min(hd.level, prev + 1)
        prev = lvl
        toc.append([lvl, hd.text, pno + 1, {"kind": fitz.LINK_GOTO, "page": pno, "to": fitz.Point(0, max(0, y - 20))}])
    doc.set_toc(toc)
    doc.set_metadata({
        "title": book.title,
        "author": ", ".join(book.authors),
        "subject": f"Offline copy of {book.base_url}",
        "creator": "book_to_pdf.py (Chromium + PyMuPDF)",
    })
    doc.save(pdf_out, garbage=3, deflate=True)
    doc.close()


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def verify(pdf_path: Path, book: Book, headings: list[Heading], page_numbers: dict[str, int]) -> list[str]:
    import pymupdf as fitz

    problems: list[str] = []
    with fitz.open(pdf_path) as doc:
        texts = [re.sub(r"\s+", " ", p.get_text()) for p in doc]
        # Cover + table of contents: every page before the first heading.
        toc_last = min(page_numbers.values(), default=1) - 2
        if any("@@" in t and re.search(r"@@[PCH]\d{5}@@", t) for t in texts):
            problems.append("layout markers leaked into the final PDF")
        for hd in headings:
            if hd.in_toc is None or hd.marker not in page_numbers:
                continue
            idx = page_numbers[hd.marker] - 1
            # Compare without numbering and whitespace ("Chapter 2" is printed via CSS).
            words = [w for w in hd.text.split() if not re.fullmatch(r"[\dA-Z]+(\.\d+)*", w)]
            probe = "".join(words)[:30]
            page_text = re.sub(r"\s+", "", texts[idx]) if idx < len(texts) else ""
            if idx <= toc_last or probe not in page_text:
                problems.append(f"heading '{hd.text}' not found on page {idx + 1}")
        print(f"  final PDF: {len(doc)} pages, {len(doc.get_toc())} bookmarks, "
              f"{sum(len(p.get_images()) for p in doc)} image placements")
    return problems


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="https://otexts.com/fpppy/", help="book landing page")
    ap.add_argument("--out", default="fpppy.pdf", help="output PDF path")
    ap.add_argument("--cache-dir", default=".book_cache", help="where downloads and intermediate files go")
    ap.add_argument("--page-size", default="A4", choices=["A4", "Letter", "A5", "Legal"])
    ap.add_argument("--toc-depth", type=int, default=2, choices=[1, 2, 3],
                    help="1 = chapters, 2 = + sections, 3 = + subsections")
    ap.add_argument("--delay", type=float, default=0.3, help="seconds between downloads")
    ap.add_argument("--max-pages", type=int, default=0, help="only convert the first N pages (testing)")
    ap.add_argument("--mathjax", default=None, help="path or URL of MathJax tex-svg.js")
    ap.add_argument("--chromium", default=None, help="path to a Chromium executable")
    ap.add_argument("--no-google-fonts", action="store_true", help="use only locally installed fonts")
    args = ap.parse_args(argv)

    base_url = args.url if args.url.endswith(("/", ".html")) else args.url + "/"
    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(cache, args.delay)

    print(f"[1/6] Reading table of contents from {base_url}")
    index_html = fetcher.page(norm_url(base_url))
    book = discover_structure(index_html, base_url)
    if args.max_pages:
        book.pages = book.pages[: args.max_pages]
    print(f"      '{book.title}' by {', '.join(book.authors) or 'unknown'}: "
          f"{len(book.pages)} pages in {len(book.parts)} parts")
    if len(book.pages) < 2:
        print("ERROR: could not find the book's table of contents.", file=sys.stderr)
        return 1

    print("[2/6] Downloading and cleaning pages")
    if book.cover_img:
        local = fetcher.asset(book.cover_img)
        book.cover_img = local or book.cover_img
    for i, page in enumerate(book.pages):
        raw = fetcher.page(page.url)
        prepare_page(page, raw, book, fetcher, is_index=(i == 0))
    rewrite_links(book)

    mathjax_src = find_mathjax(cache, args.mathjax)
    print(f"      MathJax: {mathjax_src}")

    print("[3/6] Layout pass (locating headings)")
    html1, headings, chapters = assemble_html(book, mathjax_src, True, {}, args.page_size, args.toc_depth,
                                              not args.no_google_fonts)
    (cache / "book_pass1.html").write_text(html1, encoding="utf-8")
    render_pdf(cache / "book_pass1.html", cache / "pass1.pdf", args.page_size, args.chromium)
    positions = locate_markers(cache / "pass1.pdf")
    missing = [h.text for h in headings if h.marker not in positions]
    if missing:
        print(f"  WARNING: {len(missing)} heading(s) not located, e.g. {missing[:3]}")
    page_numbers = {m: pos[0] + 1 for m, pos in positions.items()}

    print("[4/6] Final pass (with page numbers in the table of contents)")
    html2, headings2, _ = assemble_html(book, mathjax_src, False, page_numbers, args.page_size, args.toc_depth,
                                        not args.no_google_fonts)
    html_path = cache / "book.html"
    html_path.write_text(html2, encoding="utf-8")
    render_pdf(html_path, cache / "pass2.pdf", args.page_size, args.chromium)

    print("[5/6] Adding running headers, page numbers, bookmarks")
    postprocess(cache / "pass2.pdf", Path(args.out), book, headings, chapters, positions)

    print("[6/6] Verifying")
    problems = verify(Path(args.out), book, headings, page_numbers)
    for p in problems[:20]:
        print(f"  WARNING: {p}")
    print(f"Done: {args.out}" + (" (with warnings)" if problems else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
