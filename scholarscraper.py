#!/usr/bin/env python3
"""
scholarscraper.py — Cross-platform Google Scholar publication scraper.

Scrapes a Google Scholar profile, extracts publications, resolves DOIs via
CrossRef, downloads open-access PDFs via Unpaywall, collects BibTeX entries,
and compiles a clickable LaTeX publication list into a PDF.

Works on Linux, macOS, and Windows (Python 3.7+).

Usage:
    python scholarscraper.py <SCHOLAR_ID> [OPTIONS]

Examples:
    python scholarscraper.py r1tm9b4AAAAJ
    python scholarscraper.py r1tm9b4AAAAJ -o ~/my_pubs --skip-doi
    python scholarscraper.py r1tm9b4AAAAJ --unpaywall-email me@uni.edu --debug

Dependencies:
    Required:  Python 3.7+
    Optional:  beautifulsoup4 (pip install beautifulsoup4) — better HTML parsing
    Optional:  pdflatex (texlive / MiKTeX) — for compiling the PDF

If pdflatex is not available, the .tex file is still generated and can be
compiled manually or uploaded to Overleaf.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
#  Global state
# ─────────────────────────────────────────────────────────────────────────────
VERSION = "0.1.0"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEBUG = False
INTERRUPTED = False

# ─────────────────────────────────────────────────────────────────────────────
#  Coloured terminal output (works on Windows 10+ and all Unix)
# ─────────────────────────────────────────────────────────────────────────────
_COLORS_ENABLED = True

def _init_colors():
    """Enable ANSI colours on Windows 10+ by setting the console mode."""
    global _COLORS_ENABLED
    if platform.system() != "Windows":
        _COLORS_ENABLED = sys.stdout.isatty()
        return
    _COLORS_ENABLED = sys.stdout.isatty()
    if not _COLORS_ENABLED:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # STD_OUTPUT_HANDLE = -11, ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        _COLORS_ENABLED = False

_init_colors()

def _c(code: str, text: str) -> str:
    if _COLORS_ENABLED:
        return f"\033[{code}m{text}\033[0m"
    return text

def red(msg: str):    print(_c("1;31", msg))
def green(msg: str):  print(_c("1;32", msg))
def yellow(msg: str): print(_c("1;33", msg))
def blue(msg: str):   print(_c("1;34", msg))
def dbg(msg: str):
    if DEBUG:
        print(_c("0;36", f"  [DEBUG] {msg}"), file=sys.stderr, flush=True)

def banner(title: str):
    blue("╔══════════════════════════════════════════════════════════════╗")
    blue(f"║  {title:<59}║")
    blue("╚══════════════════════════════════════════════════════════════╝")

# ─────────────────────────────────────────────────────────────────────────────
#  Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

def clean(text: str) -> str:
    """Strip HTML tags, unescape entities, normalise whitespace."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def http_get(url: str, headers: Optional[dict] = None,
             timeout: int = 20) -> Optional[bytes]:
    """Simple HTTP GET returning bytes, or None on failure."""
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        dbg(f"HTTP GET failed for {url}: {e}")
        return None

def json_get(url: str, timeout: int = 15) -> Optional[dict]:
    """HTTP GET expecting JSON."""
    data = http_get(url, headers={
        "User-Agent": "ScholarScraper/1.0 (mailto:scholarscraper@example.com)",
        "Accept": "application/json",
    }, timeout=timeout)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return None

def which(cmd: str) -> Optional[str]:
    """Cross-platform shutil.which."""
    return shutil.which(cmd)

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1 — Scrape Google Scholar
# ─────────────────────────────────────────────────────────────────────────────

def check_page_health(content: str, page_num: int) -> str:
    lower = content.lower()
    if "captcha" in lower or "recaptcha" in lower or "unusual traffic" in lower:
        red(f"  ⚠ Page {page_num}: CAPTCHA / rate-limit detected!")
        return "captcha"
    if "consent.google.com" in lower or "before you continue" in lower:
        red(f"  ⚠ Page {page_num}: Google consent page detected!")
        return "consent"
    if len(content.strip()) < 500:
        yellow(f"  ⚠ Page {page_num}: Very short response ({len(content)} bytes)")
        return "empty"
    return "ok"

# ── BeautifulSoup parser ──

def parse_with_bs4(html_content: str) -> List[Dict]:
    from bs4 import BeautifulSoup  # type: ignore
    soup = BeautifulSoup(html_content, "html.parser")
    articles: List[Dict] = []

    rows = soup.select("tr.gsc_a_tr")
    dbg(f"BS4: found {len(rows)} tr.gsc_a_tr rows")

    if not rows:
        table = soup.find("table", id="gsc_a_t")
        if table:
            rows = table.find_all("tr")
            dbg(f"BS4: found {len(rows)} rows in #gsc_a_t")

    # Filter hidden header rows
    rows = [r for r in rows if r.get("aria-hidden") != "true"]
    dbg(f"BS4: {len(rows)} visible rows after filtering aria-hidden")

    for row in rows:
        article: Dict[str, Any] = {}

        td_title = row.find("td", class_="gsc_a_t")
        if td_title:
            link = td_title.find("a")
            if link:
                article["title"] = clean(link.get_text())
                href = link.get("href", "")
                if href.startswith("/"):
                    href = "https://scholar.google.com" + href
                elif not href.startswith("http"):
                    href = "https://scholar.google.com/" + href
                article["scholar_link"] = href
        else:
            link = row.find("a", class_="gsc_a_at")
            if link:
                article["title"] = clean(link.get_text())
                href = link.get("href", "")
                if href.startswith("/"):
                    href = "https://scholar.google.com" + href
                article["scholar_link"] = href

        if "title" not in article or not article["title"]:
            continue

        container = td_title if td_title else row
        grays = container.find_all("div", class_="gs_gray")
        if len(grays) >= 1:
            article["authors"] = clean(grays[0].get_text())
        if len(grays) >= 2:
            article["venue"] = clean(grays[1].get_text())

        td_year = row.find("td", class_="gsc_a_y")
        if td_year:
            ym = re.search(r"(\d{4})", td_year.get_text())
            article["year"] = ym.group(1) if ym else ""
        else:
            ys = row.find("span", class_=re.compile(r"gsc_a_h"))
            if ys:
                ym = re.search(r"(\d{4})", ys.get_text())
                article["year"] = ym.group(1) if ym else ""
            else:
                article["year"] = ""

        td_cite = row.find("td", class_="gsc_a_c")
        cite_text = ""
        if td_cite:
            cl = td_cite.find("a")
            cite_text = clean(cl.get_text()) if cl else clean(td_cite.get_text())
        else:
            cl = row.find("a", class_=re.compile(r"gsc_a_ac"))
            if cl:
                cite_text = clean(cl.get_text())
        cm = re.search(r"(\d+)", cite_text)
        article["citations"] = int(cm.group(1)) if cm else 0

        articles.append(article)

    return articles

# ── Regex fallback parser ──

def parse_with_regex(html_content: str) -> List[Dict]:
    articles: List[Dict] = []

    # Strategy 1
    rows = re.findall(r"<tr\s[^>]*?gsc_a_tr[^>]*?>(.*?)</tr>", html_content, re.DOTALL)
    dbg(f"Regex strategy 1 (gsc_a_tr): {len(rows)} rows")

    # Strategy 2
    if not rows:
        tm = re.search(r'<table[^>]*?id=["\']?gsc_a_t["\']?[^>]*?>(.*?)</table>',
                        html_content, re.DOTALL)
        if tm:
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tm.group(1), re.DOTALL)
            dbg(f"Regex strategy 2 (inside #gsc_a_t): {len(rows)} rows")

    # Strategy 3 — bare title links
    if not rows:
        links = list(re.finditer(r"<a\s[^>]*?gsc_a_at[^>]*?>(.*?)</a>",
                                  html_content, re.DOTALL))
        dbg(f"Regex strategy 3 (bare links): {len(links)}")
        for m in links:
            tag_ctx = html_content[max(0, m.start() - 200):m.end() + 50]
            href_m = re.search(r'href="([^"]*)"', tag_ctx)
            href = href_m.group(1) if href_m else ""
            title = clean(m.group(1))
            if title:
                if href.startswith("/"):
                    href = "https://scholar.google.com" + html_mod.unescape(href)
                articles.append({
                    "title": title, "scholar_link": href,
                    "authors": "", "venue": "", "year": "", "citations": 0,
                })
        if articles:
            _enrich_from_context(html_content, articles)
            return articles

    for row in rows:
        article: Dict[str, Any] = {}
        title_m = re.search(r"<a\s[^>]*?gsc_a_at[^>]*?>(.*?)</a>", row, re.DOTALL)
        if not title_m:
            continue
        a_tag_m = re.search(r"<a\s([^>]*?)>", row[title_m.start():])
        href = ""
        if a_tag_m:
            href_m = re.search(r'href="([^"]*)"', a_tag_m.group(1))
            if href_m:
                href = html_mod.unescape(href_m.group(1))
        article["title"] = clean(title_m.group(1))
        if href.startswith("/"):
            href = "https://scholar.google.com" + href
        article["scholar_link"] = href

        grays = re.findall(r'<div\s[^>]*?gs_gray[^>]*?>(.*?)</div>', row, re.DOTALL)
        if len(grays) >= 1:
            article["authors"] = clean(grays[0])
        if len(grays) >= 2:
            article["venue"] = clean(grays[1])

        ym = (re.search(r'class="gsc_a_y"[^>]*>.*?(\d{4})', row, re.DOTALL)
              or re.search(r'gsc_a_h[^>]*>(\d{4})', row, re.DOTALL)
              or re.search(r">(\d{4})</", row))
        article["year"] = ym.group(1) if ym else ""

        cm = (re.search(r'gsc_a_ac[^>]*>(\d+)<', row)
              or re.search(r'class="gsc_a_c"[^>]*>.*?(\d+)', row, re.DOTALL))
        article["citations"] = int(cm.group(1)) if cm else 0

        articles.append(article)

    return articles

def _enrich_from_context(html_content: str, articles: List[Dict]):
    for art in articles:
        title = art.get("title", "")
        if not title:
            continue
        m = re.search(re.escape(title[:40]), html_content)
        if not m:
            continue
        ctx = html_content[m.end():m.end() + 1000]
        grays = re.findall(r'<div\s[^>]*?gs_gray[^>]*?>(.*?)</div>', ctx, re.DOTALL)
        if len(grays) >= 1 and not art.get("authors"):
            art["authors"] = clean(grays[0])
        if len(grays) >= 2 and not art.get("venue"):
            art["venue"] = clean(grays[1])
        ym = re.search(r">(\d{4})</", ctx)
        if ym and not art.get("year"):
            art["year"] = ym.group(1)

def fetch_scholar_pages(scholar_id: str, data_dir: Path, cookie: str = "",
                        max_pages: int = 10, pagesize: int = 100,
                        delay: float = 3.0) -> int:
    """Download Scholar profile pages. Returns number of pages fetched."""
    pages_fetched = 0
    start = 0

    for page_idx in range(max_pages):
        url = (f"https://scholar.google.com/citations?user={scholar_id}"
               f"&cstart={start}&pagesize={pagesize}&sortby=pubdate&hl=en")
        outfile = data_dir / f"scholar_page_{page_idx}.html"

        yellow(f"  Fetching page {page_idx + 1} (start={start}) ...")

        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        }
        if cookie:
            headers["Cookie"] = cookie

        raw = http_get(url, headers=headers, timeout=20)
        if raw is None:
            yellow("  HTTP request failed — stopping pagination.")
            break

        content = raw.decode("utf-8", errors="replace")
        outfile.write_text(content, encoding="utf-8")

        # Detect problems
        lower = content.lower()
        if "captcha" in lower or "recaptcha" in lower or "unusual traffic" in lower:
            red("  ✗ CAPTCHA detected! Google is rate-limiting requests.")
            red("    Wait 10–30 minutes and try again, or use a different network.")
            outfile.unlink(missing_ok=True)
            break

        if "consent.google" in lower or "before you continue" in lower:
            red("  ✗ Google consent page detected!")
            yellow("    Workaround: pass your browser cookies with --cookie:")
            yellow('      python scholarscraper.py ID --cookie "NID=...; GSP=..."')
            outfile.unlink(missing_ok=True)
            break

        if not re.search(r"gsc_a_at|gsc_a_t|gsc_a_tr", content):
            yellow(f"  No article markers found on page {page_idx + 1} — stopping.")
            outfile.unlink(missing_ok=True)
            break

        if DEBUG:
            at_count = content.count("gsc_a_at")
            tr_count = content.count("gsc_a_tr")
            dbg(f"Page {page_idx}: gsc_a_at={at_count}, gsc_a_tr={tr_count}")

        pages_fetched += 1
        start += pagesize
        if page_idx < max_pages - 1:
            time.sleep(delay)

    return pages_fetched

def parse_all_pages(data_dir: Path) -> List[Dict]:
    """Parse all downloaded Scholar HTML pages into article dicts."""
    use_bs4 = False
    try:
        from bs4 import BeautifulSoup  # type: ignore # noqa: F401
        use_bs4 = True
        dbg("Using BeautifulSoup parser")
    except ImportError:
        dbg("BeautifulSoup not available — using regex fallback")
        yellow("  ℹ  Tip: pip install beautifulsoup4  (more reliable parsing)")

    all_articles: List[Dict] = []
    page = 0
    total_pages = 0

    while True:
        fpath = data_dir / f"scholar_page_{page}.html"
        if not fpath.exists():
            break
        content = fpath.read_text(encoding="utf-8", errors="replace")
        total_pages += 1

        status = check_page_health(content, page)
        if status in ("captcha", "consent"):
            page += 1
            continue

        if DEBUG:
            classes = set(re.findall(r'class="([^"]*)"', content))
            scholar_classes = sorted(c for c in classes if "gsc" in c or "gs_" in c)
            dbg(f"Page {page}: {len(content)} bytes")
            dbg(f"Page {page}: Scholar classes: {scholar_classes[:20]}")
            dbg(f"Page {page}: gsc_a_tr={content.count('gsc_a_tr')}, "
                f"gsc_a_at={content.count('gsc_a_at')}, gs_gray={content.count('gs_gray')}")

        articles = parse_with_bs4(content) if use_bs4 else parse_with_regex(content)
        dbg(f"Page {page}: extracted {len(articles)} articles")

        if not articles and page > 0:
            break
        all_articles.extend(articles)
        page += 1

    if not all_articles and total_pages > 0:
        red("\n  ✗ Parser found 0 articles across all pages.")
        yellow("  Possible causes:")
        yellow("    1. Google Scholar served a consent/cookie page")
        yellow("    2. Google Scholar served a CAPTCHA")
        yellow("    3. The HTML structure has changed")
        yellow("\n  TROUBLESHOOTING:")
        yellow("    • Run with --debug to see HTML details")
        yellow(f"    • Inspect the raw HTML: {data_dir / 'scholar_page_0.html'}")
        yellow("    • Install beautifulsoup4: pip install beautifulsoup4")
        yellow('    • Pass cookies: --cookie "NID=...; GSP=..."')

    # Deduplicate by title
    seen: set = set()
    unique: List[Dict] = []
    for a in all_articles:
        key = a.get("title", "").lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(a)

    unique.sort(key=lambda a: (a.get("year", "0"), a.get("citations", 0)), reverse=True)
    return unique

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2 — Resolve DOIs & download PDFs
# ─────────────────────────────────────────────────────────────────────────────

def find_doi(title: str) -> Optional[str]:
    query = urllib.parse.quote(title)
    url = f"https://api.crossref.org/works?query.title={query}&rows=3"
    data = json_get(url)
    if not data or "message" not in data:
        return None
    items = data["message"].get("items", [])
    title_lower = title.lower().strip()
    t_words = set(re.findall(r"\w+", title_lower))
    if not t_words:
        return None
    for item in items:
        for it in item.get("title", []):
            i_words = set(re.findall(r"\w+", it.lower().strip()))
            overlap = len(t_words & i_words) / max(len(t_words), 1)
            if overlap >= 0.75:
                return item.get("DOI")
    return None

def find_pdf_url(doi: str, email: str) -> Optional[str]:
    if not email or not doi:
        return None
    url = f"https://api.unpaywall.org/v2/{doi}?email={urllib.parse.quote(email)}"
    data = json_get(url)
    if not data:
        return None
    best = data.get("best_oa_location")
    if best:
        return best.get("url_for_pdf") or best.get("url")
    return None

def download_pdf(url: str, filepath: Path) -> bool:
    raw = http_get(url, timeout=30)
    if raw and raw[:5] == b"%PDF-":
        filepath.write_bytes(raw)
        return True
    return False

def _normalise_for_match(text: str) -> set:
    """Extract lowercase alpha-numeric words for fuzzy matching."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))

def match_local_pdfs(pubs: List[Dict], include_dir: Path, pdf_dir: Path) -> int:
    """
    Match user-supplied PDFs (by filename similarity to title) and copy them
    into the output pdfs/ folder. Only fills in pdf_file for publications
    that don't already have one. Returns number of matches.
    """
    if not include_dir.is_dir():
        red(f"  ✗ --include-pdf directory does not exist: {include_dir}")
        return 0

    # Collect all PDF files recursively
    local_pdfs = list(include_dir.rglob("*.pdf"))
    if not local_pdfs:
        yellow(f"  No .pdf files found in {include_dir}")
        return 0

    yellow(f"  Found {len(local_pdfs)} PDF(s) in {include_dir}")

    # Build word-sets for each local PDF filename (without extension)
    pdf_index: List[Tuple[Path, set]] = []
    for p in local_pdfs:
        stem = p.stem  # filename without extension
        words = _normalise_for_match(stem)
        pdf_index.append((p, words))

    matched = 0
    for pub in pubs:
        # Skip if already has a PDF from Unpaywall
        if pub.get("pdf_file"):
            continue

        title = pub.get("title", "")
        if not title:
            continue

        title_words = _normalise_for_match(title)
        if len(title_words) < 2:
            continue

        # Find best match by word overlap
        best_score = 0.0
        best_path: Optional[Path] = None
        for pdf_path, pdf_words in pdf_index:
            if not pdf_words:
                continue
            overlap = len(title_words & pdf_words)
            # Score = overlap relative to the smaller set
            score = overlap / min(len(title_words), len(pdf_words))
            if score > best_score:
                best_score = score
                best_path = pdf_path

        # Threshold: at least 60% word overlap
        if best_score >= 0.60 and best_path is not None:
            safe_name = re.sub(r"[^a-zA-Z0-9]", "_", title)[:80] + ".pdf"
            dest = pdf_dir / safe_name
            if not dest.exists():
                shutil.copy2(best_path, dest)
            pub["pdf_file"] = str(dest)
            matched += 1
            short = title[:60] + ("..." if len(title) > 60 else "")
            green(f"  ✓ Matched: {short}")
            print(f"       ↳ {best_path.name} (score: {best_score:.0%})")

    return matched

def resolve_dois_and_pdfs(pubs: List[Dict], pdf_dir: Path,
                          unpaywall_email: str = "",
                          delay: float = 1.5) -> Tuple[int, int]:
    """Enrich publications with DOIs and download PDFs. Returns (doi_count, pdf_count)."""
    global INTERRUPTED
    doi_count = 0
    pdf_count = 0
    total = len(pubs)

    for idx, pub in enumerate(pubs):
        if INTERRUPTED:
            yellow("  ⚠ Interrupted — stopping DOI resolution.")
            break

        title = pub.get("title", "Untitled")
        short = title[:70] + ("..." if len(title) > 70 else "")
        print(f"  [{idx+1}/{total}] {short}", flush=True)

        doi = find_doi(title)
        pub["doi"] = doi or ""
        if doi:
            doi_count += 1
            print(f"       ↳ DOI: {doi}")
        time.sleep(delay)

        pdf_url = find_pdf_url(doi, unpaywall_email) if doi else None
        pub["pdf_url"] = pdf_url or ""

        pub["pdf_file"] = ""
        if pdf_url:
            safe_name = re.sub(r"[^a-zA-Z0-9]", "_", title)[:80] + ".pdf"
            filepath = pdf_dir / safe_name
            print(f"       ↳ Downloading PDF ...")
            if download_pdf(pdf_url, filepath):
                pub["pdf_file"] = str(filepath)
                pdf_count += 1
                green(f"       ✓ Saved: {safe_name}")
            else:
                yellow(f"       ✗ Download failed or not a valid PDF")
            time.sleep(delay)

    return doi_count, pdf_count

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2b — Collect BibTeX
# ─────────────────────────────────────────────────────────────────────────────

def make_cite_key(pub: Dict, used_keys: set) -> str:
    authors = pub.get("authors", "")
    year = pub.get("year", "XXXX") or "XXXX"
    title = pub.get("title", "untitled")

    first_author = authors.split(",")[0].strip() if authors else "Unknown"
    parts = first_author.split()
    last_name = parts[-1] if parts else "Unknown"

    skip_words = {"a","an","the","on","in","of","for","and","with","to","from","by"}
    title_word = "untitled"
    for w in re.findall(r"[A-Za-z]+", title):
        if w.lower() not in skip_words and len(w) > 2:
            title_word = w
            break

    key = f"{last_name}_{year}_{title_word}"
    key = unicodedata.normalize("NFKD", key).encode("ascii", "ignore").decode()
    key = re.sub(r"[^a-zA-Z0-9_]", "", key)

    base_key = key
    counter = 2
    while key in used_keys:
        key = f"{base_key}_{counter}"
        counter += 1
    used_keys.add(key)
    return key

def escape_bibtex(text: str) -> str:
    if not text:
        return ""
    for old, new in [("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
                     ("#", r"\#"), ("_", r"\_")]:
        text = text.replace(old, new)
    return text

def fetch_bibtex_from_doi(doi: str) -> Optional[str]:
    raw = http_get(f"https://doi.org/{doi}", headers={
        "Accept": "application/x-bibtex",
        "User-Agent": "ScholarScraper/1.0",
    }, timeout=15)
    if raw:
        bib = raw.decode("utf-8", errors="replace")
        if "@" in bib and "{" in bib:
            return bib.strip()
    return None

def generate_bibtex(pub: Dict, cite_key: str) -> str:
    title = pub.get("title", "Untitled")
    authors = pub.get("authors", "Unknown")
    year = pub.get("year", "")
    venue = pub.get("venue", "")
    doi = pub.get("doi", "")
    scholar_link = pub.get("scholar_link", "")

    author_list = [a.strip() for a in authors.split(",") if a.strip()]
    bib_authors = " and ".join(author_list)

    entry_type = "@article"
    if venue:
        vl = venue.lower()
        if any(k in vl for k in ["conference","proceedings","workshop","symposium",
                                  "icml","neurips","iclr","cvpr","iccv","eccv",
                                  "aaai","ijcai","acl","emnlp","naacl","aiaa","asme"]):
            entry_type = "@inproceedings"
        elif any(k in vl for k in ["book","springer","lecture notes","chapter"]):
            entry_type = "@incollection"
        elif any(k in vl for k in ["thesis","dissertation"]):
            entry_type = "@phdthesis"
        elif any(k in vl for k in ["arxiv","preprint"]):
            entry_type = "@misc"

    lines = [f"{entry_type}{{{cite_key},"]
    lines.append(f"  title = {{{escape_bibtex(title)}}},")
    lines.append(f"  author = {{{escape_bibtex(bib_authors)}}},")
    if year:
        lines.append(f"  year = {{{year}}},")
    if venue:
        if entry_type == "@inproceedings":
            lines.append(f"  booktitle = {{{escape_bibtex(venue)}}},")
        elif entry_type == "@article":
            lines.append(f"  journal = {{{escape_bibtex(venue)}}},")
        else:
            lines.append(f"  note = {{{escape_bibtex(venue)}}},")
    if doi:
        lines.append(f"  doi = {{{doi}}},")
    if scholar_link:
        lines.append(f"  url = {{{scholar_link}}},")
    lines.append("}")
    return "\n".join(lines)

def collect_bibtex(pubs: List[Dict], bibtex_dir: Path,
                   delay: float = 1.0) -> int:
    """Collect BibTeX for all publications. Returns count of entries."""
    global INTERRUPTED
    all_bibtex: List[str] = []
    used_keys: set = set()
    stats = {"crossref": 0, "generated": 0}
    total = len(pubs)

    for idx, pub in enumerate(pubs):
        if INTERRUPTED:
            yellow("  ⚠ Interrupted — saving partial BibTeX.")
            break

        title = pub.get("title", "Untitled")
        doi = pub.get("doi", "")
        short = title[:65] + ("..." if len(title) > 65 else "")
        print(f"  [{idx+1}/{total}] {short}", flush=True)

        cite_key = make_cite_key(pub, used_keys)
        bib_entry = None

        if doi:
            bib_entry = fetch_bibtex_from_doi(doi)
            if bib_entry:
                bib_entry = re.sub(r"(@\w+)\{[^,]+,",
                                   rf"\1{{{cite_key},", bib_entry, count=1)
                stats["crossref"] += 1
                print(f"       ✓ BibTeX from CrossRef")
            time.sleep(delay)

        if not bib_entry:
            bib_entry = generate_bibtex(pub, cite_key)
            stats["generated"] += 1
            print(f"       ↳ BibTeX generated from metadata")

        # Individual file (.bib for tools, .txt for clickable PDF link)
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", cite_key)[:80]
        bib_name = safe_name + ".bib"
        txt_name = safe_name + ".txt"
        (bibtex_dir / bib_name).write_text(bib_entry + "\n", encoding="utf-8")
        (bibtex_dir / txt_name).write_text(bib_entry + "\n", encoding="utf-8")

        # Store filenames on the pub for LaTeX references
        # .txt is used for the clickable link (opens in text editor on all OS)
        pub["bib_file"] = bib_name
        pub["bib_txt_file"] = txt_name
        pub["cite_key"] = cite_key

        all_bibtex.append(bib_entry)

    # Combined file
    combined = bibtex_dir / "publications.bib"
    combined.write_text(
        f"% Combined BibTeX file — auto-generated by scholarscraper.py\n"
        f"% {len(all_bibtex)} entries\n\n"
        + "\n\n".join(all_bibtex) + "\n",
        encoding="utf-8",
    )

    green(f"\n  Summary: {stats['crossref']} from CrossRef, "
          f"{stats['generated']} generated from metadata")
    return len(all_bibtex)

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3 — Generate LaTeX
# ─────────────────────────────────────────────────────────────────────────────

def tex_escape(text: str) -> str:
    if not text:
        return ""
    for old, new in [("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"),
                     ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
                     ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}")]:
        text = text.replace(old, new)
    return text

def generate_latex(pubs: List[Dict], tex_path: Path, scholar_id: str):
    """Generate the LaTeX publication list."""
    by_year: Dict[str, List[Dict]] = {}
    for pub in pubs:
        year = pub.get("year", "") or "Undated"
        by_year.setdefault(year, []).append(pub)

    sorted_years = sorted(by_year.keys(),
                          key=lambda y: y if y != "Undated" else "0000",
                          reverse=True)

    # Auto-detect author name for bolding
    author_name = ""
    if pubs:
        firsts = []
        for p in pubs[:15]:
            a = p.get("authors", "")
            if a:
                f = a.split(",")[0].strip()
                if f:
                    firsts.append(f)
        if firsts:
            author_name = Counter(firsts).most_common(1)[0][0]

    total_citations = sum(p.get("citations", 0) for p in pubs)

    L = []  # lines
    L.append(r"""\documentclass[11pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage[margin=2.5cm]{geometry}
\usepackage[dvipsnames]{xcolor}
\usepackage{hyperref}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage{fancyhdr}
\usepackage{lastpage}
\usepackage{parskip}

\hypersetup{
    colorlinks=true,
    linkcolor=NavyBlue,
    urlcolor=NavyBlue,
    citecolor=NavyBlue,
    pdftitle={Publication List},
    pdfauthor={Generated by scholarscraper.py},
}

\titleformat{\section}
  {\Large\bfseries\color{NavyBlue}}
  {}{0em}{}
  [\vspace{-0.5em}\textcolor{NavyBlue}{\rule{\textwidth}{0.4pt}}]

\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0pt}
\fancyfoot[C]{\small\textcolor{gray}{Page \thepage\ of \pageref{LastPage}}}

\begin{document}

\begin{center}
  {\LARGE\bfseries Publication List} \\[0.5em]
""")

    if scholar_id:
        L.append(r"  {\small\href{https://scholar.google.com/citations?user="
                 + scholar_id + r"}{Google Scholar Profile}} \\[0.3em]")

    L.append(r"  {\small\textcolor{gray}{Auto-generated \today{} ~$\cdot$~ "
             + tex_escape(str(len(pubs))) + r" publications ~$\cdot$~ "
             + tex_escape(str(total_citations)) + r" total citations}}")
    L.append(r"""\end{center}
\vspace{1em}

{\small
\textcolor{NavyBlue}{[\,DOI\,]} = link to publisher ~~$\cdot$~~
\textcolor{ForestGreen}{[\,PDF\,]} = downloaded open-access PDF ~~$\cdot$~~
\textcolor{Bittersweet}{[\,BIB\,]} = BibTeX entry ~~$\cdot$~~
\textcolor{gray}{[\,Scholar\,]} = Google Scholar page
}
\vspace{1em}
""")

    pub_number = len(pubs)

    for year in sorted_years:
        L.append(r"\section*{" + tex_escape(year) + "}")
        year_pubs = sorted(by_year[year],
                           key=lambda p: p.get("citations", 0), reverse=True)
        start_num = pub_number - len(year_pubs) + 1
        L.append(r"\begin{enumerate}[label={\textbf{[\arabic*]}},start="
                 + str(start_num) + r",leftmargin=2.5em]")

        for pub in year_pubs:
            title = tex_escape(pub.get("title", "Untitled"))
            authors = tex_escape(pub.get("authors", ""))
            venue = tex_escape(pub.get("venue", ""))
            doi = pub.get("doi", "")
            pdf_file = pub.get("pdf_file", "")
            bib_txt_file = pub.get("bib_txt_file", "")
            scholar_link = pub.get("scholar_link", "")
            citations = pub.get("citations", 0)

            if author_name:
                esc_name = tex_escape(author_name)
                authors = authors.replace(esc_name, r"\textbf{" + esc_name + "}")

            iL = [r"  \item \textbf{" + title + r"}"]
            if authors:
                iL.append(r"  \\ " + authors)
            if venue:
                iL.append(r"  \\ \textit{" + venue + r"}")

            links = []
            if doi:
                links.append(r"\href{https://doi.org/" + doi
                             + r"}{\textcolor{NavyBlue}{\small[\,DOI\,]}}")
            if pdf_file:
                rel = os.path.basename(pdf_file)
                links.append(r"\href{./pdfs/" + rel
                             + r"}{\textcolor{ForestGreen}{\small[\,PDF\,]}}")
            if bib_txt_file:
                links.append(r"\href{./bibtex/" + bib_txt_file
                             + r"}{\textcolor{Bittersweet}{\small[\,BIB\,]}}")
            if scholar_link:
                esc = scholar_link.replace("%", r"\%").replace("#", r"\#").replace("&", r"\&")
                links.append(r"\href{" + esc
                             + r"}{\textcolor{gray}{\small[\,Scholar\,]}}")
            if citations > 0:
                links.append(r"{\small\textcolor{gray}{" + str(citations) + r" citations}}")

            if links:
                iL.append(r"  \\ " + " ~~ ".join(links))

            L.append("\n".join(iL))
            L.append("")

        L.append(r"\end{enumerate}")
        L.append("")
        pub_number -= len(year_pubs)

    L.append(r"\end{document}")

    tex_path.write_text("\n".join(L), encoding="utf-8")

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4 — Compile LaTeX → PDF
# ─────────────────────────────────────────────────────────────────────────────

def compile_latex(output_dir: Path) -> bool:
    """Run pdflatex twice. Returns True on success."""
    pdflatex = which("pdflatex")
    if not pdflatex:
        yellow("  pdflatex not found — skipping PDF compilation.")
        yellow("  Install TeX Live (Linux/Mac) or MiKTeX (Windows),")
        yellow("  or upload publications.tex to Overleaf.")
        return False

    tex_file = "publications.tex"
    for pass_num in (1, 2):
        yellow(f"  Running pdflatex (pass {pass_num}/2) ...")
        try:
            subprocess.run(
                [pdflatex, "-interaction=nonstopmode", "-halt-on-error", tex_file],
                cwd=str(output_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            red(f"  pdflatex failed: {e}")
            return False

    pdf_path = output_dir / "publications.pdf"
    if pdf_path.exists():
        green(f"  ✓ PDF compiled successfully: {pdf_path}")
        return True
    else:
        red(f"  ✗ PDF compilation failed. Check {output_dir / 'publications.log'}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
#  Signal handling
# ─────────────────────────────────────────────────────────────────────────────

def _sigint_handler(sig, frame):
    global INTERRUPTED
    INTERRUPTED = True
    print()
    yellow("  ⚠ Ctrl+C detected — finishing current step and continuing ...")

# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global DEBUG

    parser = argparse.ArgumentParser(
        description="Scrape Google Scholar → BibTeX + clickable LaTeX PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scholarscraper.py r1tm9b4AAAAJ
  python scholarscraper.py r1tm9b4AAAAJ -o ~/my_pubs
  python scholarscraper.py r1tm9b4AAAAJ --skip-doi --debug
  python scholarscraper.py r1tm9b4AAAAJ --unpaywall-email me@uni.edu
  python scholarscraper.py r1tm9b4AAAAJ --include-pdf ~/Papers
  python scholarscraper.py r1tm9b4AAAAJ --cookie "NID=...; GSP=..."
""")

    parser.add_argument("scholar_id",
                        help='Google Scholar user ID (from ?user=XXX in your profile URL)')
    parser.add_argument("-o", "--output", default="./scholar_output",
                        help="Output directory (default: ./scholar_output)")
    parser.add_argument("--skip-doi", action="store_true",
                        help="Skip DOI resolution and PDF downloads (fast mode)")
    parser.add_argument("--skip-bibtex", action="store_true",
                        help="Skip BibTeX collection")
    parser.add_argument("--skip-pdf", action="store_true",
                        help="Skip LaTeX → PDF compilation")
    parser.add_argument("--include-pdf", metavar="DIR",
                        help="Directory of your own PDFs to match with publications. "
                             "Files are matched by fuzzy title similarity against filenames. "
                             "Matched PDFs are copied to the output pdfs/ folder.")
    parser.add_argument("--unpaywall-email", default="",
                        help="Email for Unpaywall API (enables PDF lookup via DOI)")
    parser.add_argument("--cookie", default="",
                        help="Browser cookie string for Scholar (bypasses consent pages)")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="Delay between Scholar page fetches in seconds (default: 3)")
    parser.add_argument("--max-pages", type=int, default=10,
                        help="Max Scholar profile pages to fetch (default: 10)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose diagnostic output")
    parser.add_argument("--version", action="version", version=f"scholarscraper {VERSION}")

    args = parser.parse_args()
    DEBUG = args.debug

    # Also check environment variables (compatible with the bash script)
    unpaywall_email = args.unpaywall_email or os.environ.get("UNPAYWALL_EMAIL", "")
    cookie = args.cookie or os.environ.get("SCHOLAR_COOKIE", "")
    skip_doi = args.skip_doi or os.environ.get("SKIP_DOI", "0") == "1"

    # Setup directories
    output_dir = Path(args.output).resolve()
    pdf_dir = output_dir / "pdfs"
    bibtex_dir = output_dir / "bibtex"
    data_dir = output_dir / "data"

    for d in (pdf_dir, bibtex_dir, data_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Install signal handler
    signal.signal(signal.SIGINT, _sigint_handler)

    # ══════════════════ STEP 1: SCRAPE ══════════════════
    banner("Step 1 — Fetching publications from Google Scholar")

    pages = fetch_scholar_pages(
        args.scholar_id, data_dir, cookie=cookie,
        max_pages=args.max_pages, pagesize=100, delay=args.delay,
    )
    green(f"  Fetched {pages} page(s).")

    if pages == 0:
        red("  Could not fetch any usable pages. Check your network and Scholar ID.")
        sys.exit(1)

    yellow("  Parsing publications ...")
    pubs = parse_all_pages(data_dir)
    pub_count = len(pubs)
    green(f"  Found {pub_count} unique publication(s).")

    # Save raw parsed data
    (data_dir / "publications.json").write_text(
        json.dumps(pubs, ensure_ascii=False, indent=2), encoding="utf-8")

    if pub_count == 0:
        red("  No publications found.")
        yellow(f"  Run with --debug for diagnostics.")
        yellow(f"  Or inspect: {data_dir / 'scholar_page_0.html'}")
        sys.exit(1)

    # ══════════════════ STEP 2: DOIs & PDFs ══════════════════
    banner("Step 2 — Looking for DOIs and open-access PDFs")

    doi_count = 0
    pdf_count = 0

    if skip_doi:
        yellow("  Skipping DOI/PDF resolution (--skip-doi).")
    else:
        yellow(f"  Resolving DOIs for {pub_count} publications ...")
        yellow(f"  (≈{pub_count * 2 // 60} min — press Ctrl+C to skip)")
        print()
        doi_count, pdf_count = resolve_dois_and_pdfs(
            pubs, pdf_dir, unpaywall_email=unpaywall_email)
        green(f"  DOIs found: {doi_count}/{pub_count}")
        green(f"  PDFs downloaded: {pdf_count}/{pub_count}")

    # ── Match local PDFs if --include-pdf was given ──
    if args.include_pdf:
        banner("Step 2a — Matching local PDFs")
        include_dir = Path(args.include_pdf).resolve()
        local_matched = match_local_pdfs(pubs, include_dir, pdf_dir)
        total_with_pdf = sum(1 for p in pubs if p.get("pdf_file"))
        green(f"  Matched {local_matched} local PDF(s)")
        green(f"  Total publications with PDFs: {total_with_pdf}/{pub_count}")

    # Save enriched data
    (data_dir / "publications_enriched.json").write_text(
        json.dumps(pubs, ensure_ascii=False, indent=2), encoding="utf-8")

    # Reset interrupt flag for next step
    global INTERRUPTED
    INTERRUPTED = False

    # ══════════════════ STEP 2b: BibTeX ══════════════════
    banner("Step 2b — Collecting BibTeX entries")

    bib_count = 0
    if args.skip_bibtex:
        yellow("  Skipping BibTeX collection (--skip-bibtex).")
    else:
        yellow(f"  Fetching BibTeX for {pub_count} publications ...")
        yellow(f"  (CrossRef lookup for DOIs + generated entries for the rest)")
        print()
        bib_count = collect_bibtex(pubs, bibtex_dir)
        green(f"  ✓ {bib_count} .bib files saved to {bibtex_dir}/")
        if (bibtex_dir / "publications.bib").exists():
            green(f"  ✓ Combined file: {bibtex_dir / 'publications.bib'}")

    INTERRUPTED = False

    # ══════════════════ STEP 3: LaTeX ══════════════════
    banner("Step 3 — Generating LaTeX publication list")

    tex_path = output_dir / "publications.tex"
    generate_latex(pubs, tex_path, args.scholar_id)
    green(f"  TeX file created: {tex_path}")

    # ══════════════════ STEP 4: Compile ══════════════════
    if not args.skip_pdf:
        banner("Step 4 — Compiling LaTeX → PDF")
        compile_latex(output_dir)
    else:
        yellow("  Skipping PDF compilation (--skip-pdf).")

    # ══════════════════ SUMMARY ══════════════════
    print()
    banner("Done!")
    print()
    tree = f"""  Output directory:  {output_dir}/
  ├── publications.pdf       ← Your clickable publication list
  ├── publications.tex       ← LaTeX source (customise as needed)
  ├── bibtex/
  │   ├── publications.bib   ← All entries in one file
  │   └── *.bib              ← One file per publication
  ├── pdfs/                  ← Downloaded open-access PDFs
  └── data/
      ├── publications.json  ← Raw parsed data
      └── publications_enriched.json  ← With DOIs & PDF links"""
    print(tree)
    print()

    if pdf_count > 0:
        green(f"  {pdf_count} PDF(s) were downloaded to {pdf_dir}/")
    if unpaywall_email:
        print(f"  Unpaywall was used for PDF lookups (email: {unpaywall_email})")
    elif not skip_doi:
        yellow("  Tip: Use --unpaywall-email your@email.com to enable PDF downloads")
    print()


if __name__ == "__main__":
    main()
