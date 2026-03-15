#!/usr/bin/env python3
"""
scholarscraper_gui.py — Web-based GUI for the Google Scholar scraper.

Launches a local web server and opens a dashboard in your default browser.
No external dependencies — uses only Python 3.7+ stdlib.

Usage:
    python scholarscraper_gui.py [--port 8457]

Then open http://localhost:8457 in your browser (auto-opens on launch).
"""

from __future__ import annotations

import html as html_mod
import http.server
import json
import os
import platform
import queue
import re
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────────────────────
VERSION = "0.1.0"
DEFAULT_PORT = 8457
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ─────────────────────────────────────────────────────────────────────────────
#  Global state shared between server threads
# ─────────────────────────────────────────────────────────────────────────────
state = {
    "status": "idle",          # idle | scraping | resolving | bibtex | latex | done | error
    "progress": 0,             # 0-100
    "progress_label": "",
    "publications": [],        # list of pub dicts
    "log": [],                 # list of log line strings
    "output_dir": "",
    "error": "",
    "scholar_id": "",
    "stats": {
        "total": 0, "dois": 0, "pdfs": 0, "bibtex": 0,
        "total_citations": 0,
    },
}
state_lock = threading.Lock()
sse_queues: List[queue.Queue] = []  # one per connected SSE client


def log(msg: str):
    with state_lock:
        state["log"].append(msg)
    for q in sse_queues:
        try:
            q.put_nowait(("log", msg))
        except queue.Full:
            pass


def update_state(**kwargs):
    with state_lock:
        state.update(kwargs)
    # Push state change to SSE clients
    snapshot = get_state_snapshot()
    for q in sse_queues:
        try:
            q.put_nowait(("state", json.dumps(snapshot)))
        except queue.Full:
            pass


def get_state_snapshot() -> dict:
    with state_lock:
        return {
            "status": state["status"],
            "progress": state["progress"],
            "progress_label": state["progress_label"],
            "stats": dict(state["stats"]),
            "error": state["error"],
            "scholar_id": state["scholar_id"],
            "output_dir": state["output_dir"],
            "pub_count": len(state["publications"]),
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Scraper core (adapted from scholarscraper.py — self-contained)
# ─────────────────────────────────────────────────────────────────────────────

def clean(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", str(text))
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def http_get(url: str, headers: Optional[dict] = None, timeout: int = 20) -> Optional[bytes]:
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def json_get(url: str, timeout: int = 15) -> Optional[dict]:
    data = http_get(url, headers={
        "User-Agent": "ScholarScraper/1.0",
        "Accept": "application/json",
    }, timeout=timeout)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return None


# ── Parsers ──

def parse_with_bs4(html_content: str) -> List[Dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, "html.parser")
    rows = soup.select("tr.gsc_a_tr")
    if not rows:
        table = soup.find("table", id="gsc_a_t")
        if table:
            rows = table.find_all("tr")
    rows = [r for r in rows if r.get("aria-hidden") != "true"]
    articles = []
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
            article["year"] = ""
        td_cite = row.find("td", class_="gsc_a_c")
        cite_text = ""
        if td_cite:
            cl = td_cite.find("a")
            cite_text = clean(cl.get_text()) if cl else clean(td_cite.get_text())
        cm = re.search(r"(\d+)", cite_text)
        article["citations"] = int(cm.group(1)) if cm else 0
        articles.append(article)
    return articles


def parse_with_regex(html_content: str) -> List[Dict]:
    articles = []
    rows = re.findall(r"<tr\s[^>]*?gsc_a_tr[^>]*?>(.*?)</tr>", html_content, re.DOTALL)
    if not rows:
        tm = re.search(r'<table[^>]*?id=["\']?gsc_a_t["\']?[^>]*?>(.*?)</table>',
                        html_content, re.DOTALL)
        if tm:
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tm.group(1), re.DOTALL)
    if not rows:
        for m in re.finditer(r"<a\s[^>]*?gsc_a_at[^>]*?>(.*?)</a>", html_content, re.DOTALL):
            tag_ctx = html_content[max(0, m.start()-200):m.end()+50]
            href_m = re.search(r'href="([^"]*)"', tag_ctx)
            href = href_m.group(1) if href_m else ""
            title = clean(m.group(1))
            if title:
                if href.startswith("/"):
                    href = "https://scholar.google.com" + html_mod.unescape(href)
                articles.append({"title": title, "scholar_link": href,
                                 "authors": "", "venue": "", "year": "", "citations": 0})
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


# ── DOI / PDF / BibTeX helpers ──

def find_doi(title: str) -> Optional[str]:
    query = urllib.parse.quote(title)
    url = f"https://api.crossref.org/works?query.title={query}&rows=3"
    data = json_get(url)
    if not data or "message" not in data:
        return None
    t_words = set(re.findall(r"\w+", title.lower().strip()))
    if not t_words:
        return None
    for item in data["message"].get("items", []):
        for it in item.get("title", []):
            i_words = set(re.findall(r"\w+", it.lower().strip()))
            if len(t_words & i_words) / max(len(t_words), 1) >= 0.75:
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


def fetch_bibtex_from_doi(doi: str) -> Optional[str]:
    raw = http_get(f"https://doi.org/{doi}", headers={
        "Accept": "application/x-bibtex", "User-Agent": "ScholarScraper/1.0",
    }, timeout=15)
    if raw:
        bib = raw.decode("utf-8", errors="replace")
        if "@" in bib and "{" in bib:
            return bib.strip()
    return None


def escape_bibtex(text: str) -> str:
    if not text:
        return ""
    for old, new in [("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
                     ("#", r"\#"), ("_", r"\_")]:
        text = text.replace(old, new)
    return text


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


def tex_escape(text: str) -> str:
    if not text:
        return ""
    for old, new in [("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"),
                     ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
                     ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}")]:
        text = text.replace(old, new)
    return text


# ─────────────────────────────────────────────────────────────────────────────
#  Main scraper pipeline (runs in background thread)
# ─────────────────────────────────────────────────────────────────────────────

def run_scraper(scholar_id: str, output_dir_str: str, cookie: str = "",
                unpaywall_email: str = "", skip_doi: bool = False,
                include_pdf_dir: str = ""):
    """Full scraper pipeline — runs in a background thread."""
    try:
        output_dir = Path(output_dir_str).resolve()
        pdf_dir = output_dir / "pdfs"
        bibtex_dir = output_dir / "bibtex"
        data_dir = output_dir / "data"
        for d in (pdf_dir, bibtex_dir, data_dir):
            d.mkdir(parents=True, exist_ok=True)

        update_state(status="scraping", progress=0, error="",
                     scholar_id=scholar_id, output_dir=str(output_dir))

        # ── Step 1: Scrape Scholar ──
        log("═══ Step 1: Fetching Google Scholar profile ═══")
        use_bs4 = False
        try:
            from bs4 import BeautifulSoup  # noqa
            use_bs4 = True
            log("Using BeautifulSoup parser")
        except ImportError:
            log("Using regex parser (install beautifulsoup4 for better results)")

        all_articles: List[Dict] = []
        max_pages = 10
        pagesize = 100

        for page_idx in range(max_pages):
            start = page_idx * pagesize
            url = (f"https://scholar.google.com/citations?user={scholar_id}"
                   f"&cstart={start}&pagesize={pagesize}&sortby=pubdate&hl=en")
            pct = int((page_idx / max_pages) * 30)
            update_state(progress=pct,
                         progress_label=f"Fetching page {page_idx+1}...")
            log(f"  Fetching page {page_idx+1} (start={start})")

            headers = {"User-Agent": USER_AGENT,
                       "Accept-Language": "en-US,en;q=0.9",
                       "Accept": "text/html"}
            if cookie:
                headers["Cookie"] = cookie

            raw = http_get(url, headers=headers, timeout=20)
            if raw is None:
                log(f"  HTTP request failed — stopping.")
                break

            content = raw.decode("utf-8", errors="replace")
            (data_dir / f"scholar_page_{page_idx}.html").write_text(content, encoding="utf-8")

            lower = content.lower()
            if "captcha" in lower or "recaptcha" in lower:
                log("  ✗ CAPTCHA detected! Wait and retry, or use a different network.")
                break
            if "consent.google" in lower or "before you continue" in lower:
                log("  ✗ Google consent page! Use the Cookie field to bypass.")
                break
            if not re.search(r"gsc_a_at|gsc_a_t|gsc_a_tr", content):
                log(f"  No more articles on page {page_idx+1} — done.")
                break

            articles = parse_with_bs4(content) if use_bs4 else parse_with_regex(content)
            log(f"  Page {page_idx+1}: {len(articles)} articles")
            if not articles and page_idx > 0:
                break
            all_articles.extend(articles)
            time.sleep(2.5)

        # Deduplicate
        seen: set = set()
        pubs: List[Dict] = []
        for a in all_articles:
            key = a.get("title", "").lower().strip()
            if key and key not in seen:
                seen.add(key)
                pubs.append(a)
        pubs.sort(key=lambda a: (a.get("year", "0"), a.get("citations", 0)), reverse=True)

        with state_lock:
            state["publications"] = pubs
            state["stats"]["total"] = len(pubs)
            state["stats"]["total_citations"] = sum(p.get("citations", 0) for p in pubs)

        log(f"═══ Found {len(pubs)} unique publications ═══")
        update_state(progress=30, progress_label="Scraping complete")

        if not pubs:
            update_state(status="error", error="No publications found. Check Scholar ID or use Cookie.")
            return

        (data_dir / "publications.json").write_text(
            json.dumps(pubs, ensure_ascii=False, indent=2), encoding="utf-8")

        # ── Step 2: DOIs & PDFs ──
        if skip_doi:
            log("═══ Skipping DOI resolution (fast mode) ═══")
            for p in pubs:
                p["doi"] = p.get("doi", "")
                p["pdf_url"] = ""
                p["pdf_file"] = ""
            update_state(progress=60, progress_label="DOI step skipped")
        else:
            log("═══ Step 2: Resolving DOIs & downloading PDFs ═══")
            update_state(status="resolving")
            doi_count = 0
            pdf_count = 0
            total = len(pubs)
            for idx, pub in enumerate(pubs):
                pct = 30 + int((idx / total) * 30)
                short = pub.get("title", "?")[:60]
                update_state(progress=pct,
                             progress_label=f"DOI {idx+1}/{total}: {short}...")

                doi = find_doi(pub.get("title", ""))
                pub["doi"] = doi or ""
                if doi:
                    doi_count += 1
                    log(f"  [{idx+1}/{total}] DOI: {doi}")
                else:
                    log(f"  [{idx+1}/{total}] No DOI found")

                pdf_url = find_pdf_url(doi, unpaywall_email) if doi else None
                pub["pdf_url"] = pdf_url or ""
                pub["pdf_file"] = ""
                if pdf_url:
                    safe = re.sub(r"[^a-zA-Z0-9]", "_", pub.get("title",""))[:80] + ".pdf"
                    fp = pdf_dir / safe
                    raw_pdf = http_get(pdf_url, timeout=30)
                    if raw_pdf and raw_pdf[:5] == b"%PDF-":
                        fp.write_bytes(raw_pdf)
                        pub["pdf_file"] = str(fp)
                        pdf_count += 1
                        log(f"       ✓ PDF downloaded")
                time.sleep(1.2)

            with state_lock:
                state["stats"]["dois"] = doi_count
                state["stats"]["pdfs"] = pdf_count
            log(f"═══ DOIs: {doi_count}/{total}, PDFs: {pdf_count}/{total} ═══")
            update_state(progress=60)

        # ── Match local PDFs if include_pdf_dir was given ──
        if include_pdf_dir:
            inc_dir = Path(include_pdf_dir).resolve()
            if inc_dir.is_dir():
                log("═══ Matching local PDFs from folder ═══")
                log(f"  Scanning: {inc_dir}")
                local_pdfs = list(inc_dir.rglob("*.pdf"))
                log(f"  Found {len(local_pdfs)} PDF files")

                def norm(text):
                    return set(re.findall(r"[a-z0-9]+", text.lower()))

                pdf_index = [(p, norm(p.stem)) for p in local_pdfs]
                matched = 0
                for pub in pubs:
                    if pub.get("pdf_file"):
                        continue
                    title = pub.get("title", "")
                    if not title:
                        continue
                    title_words = norm(title)
                    if len(title_words) < 2:
                        continue
                    best_score, best_path = 0.0, None
                    for pdf_path, pdf_words in pdf_index:
                        if not pdf_words:
                            continue
                        overlap = len(title_words & pdf_words)
                        score = overlap / min(len(title_words), len(pdf_words))
                        if score > best_score:
                            best_score = score
                            best_path = pdf_path
                    if best_score >= 0.60 and best_path is not None:
                        safe = re.sub(r"[^a-zA-Z0-9]", "_", title)[:80] + ".pdf"
                        dest = pdf_dir / safe
                        if not dest.exists():
                            shutil.copy2(best_path, dest)
                        pub["pdf_file"] = str(dest)
                        matched += 1
                        log(f"  ✓ Matched: {title[:55]}... ← {best_path.name}")

                log(f"═══ Matched {matched} local PDF(s) ═══")
                total_pdfs = sum(1 for p in pubs if p.get("pdf_file"))
                with state_lock:
                    state["stats"]["pdfs"] = total_pdfs
            else:
                log(f"  ⚠ Include-PDF directory not found: {inc_dir}")

        # ── Step 2b: BibTeX ──
        log("═══ Step 3: Collecting BibTeX entries ═══")
        update_state(status="bibtex", progress_label="Collecting BibTeX...")
        used_keys: set = set()
        all_bibtex: List[str] = []
        bib_crossref = 0
        total = len(pubs)

        for idx, pub in enumerate(pubs):
            pct = 60 + int((idx / total) * 20)
            update_state(progress=pct,
                         progress_label=f"BibTeX {idx+1}/{total}")

            cite_key = make_cite_key(pub, used_keys)
            bib_entry = None
            doi = pub.get("doi", "")

            if doi:
                bib_entry = fetch_bibtex_from_doi(doi)
                if bib_entry:
                    bib_entry = re.sub(r"(@\w+)\{[^,]+,",
                                       rf"\1{{{cite_key},", bib_entry, count=1)
                    bib_crossref += 1
                time.sleep(0.8)

            if not bib_entry:
                bib_entry = generate_bibtex(pub, cite_key)

            safe_base = re.sub(r"[^a-zA-Z0-9_]", "_", cite_key)[:80]
            for ext in (".bib", ".txt"):
                (bibtex_dir / (safe_base + ext)).write_text(bib_entry + "\n", encoding="utf-8")

            pub["bib_file"] = safe_base + ".bib"
            pub["bib_txt_file"] = safe_base + ".txt"
            pub["cite_key"] = cite_key
            pub["bibtex"] = bib_entry
            all_bibtex.append(bib_entry)

        # Combined .bib
        (bibtex_dir / "publications.bib").write_text(
            f"% Combined BibTeX — {len(all_bibtex)} entries\n\n"
            + "\n\n".join(all_bibtex) + "\n", encoding="utf-8")

        with state_lock:
            state["stats"]["bibtex"] = len(all_bibtex)
            state["publications"] = pubs  # refresh with bibtex data

        log(f"═══ BibTeX: {bib_crossref} from CrossRef, {len(all_bibtex)-bib_crossref} generated ═══")
        update_state(progress=80)

        # Save enriched JSON
        (data_dir / "publications_enriched.json").write_text(
            json.dumps(pubs, ensure_ascii=False, indent=2), encoding="utf-8")

        # ── Step 4: LaTeX ──
        log("═══ Step 4: Generating LaTeX ═══")
        update_state(status="latex", progress=85, progress_label="Generating LaTeX...")

        tex_path = output_dir / "publications.tex"
        _generate_latex(pubs, tex_path, scholar_id)
        log(f"  TeX written: {tex_path}")

        # Compile if pdflatex available
        pdflatex = shutil.which("pdflatex")
        if pdflatex:
            log("  Compiling PDF (2 passes)...")
            update_state(progress=90, progress_label="Compiling PDF...")
            for _ in range(2):
                subprocess.run([pdflatex, "-interaction=nonstopmode",
                                "-halt-on-error", "publications.tex"],
                               cwd=str(output_dir),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=120)
            if (output_dir / "publications.pdf").exists():
                log("  ✓ PDF compiled successfully")
            else:
                log("  ✗ PDF compilation failed — .tex file still available")
        else:
            log("  pdflatex not found — .tex file ready for Overleaf or manual compilation")

        update_state(status="done", progress=100, progress_label="Complete!")
        log("═══ All done! ═══")

    except Exception as e:
        log(f"ERROR: {e}")
        log(traceback.format_exc())
        update_state(status="error", error=str(e))


def _generate_latex(pubs, tex_path, scholar_id):
    """Generate LaTeX (same logic as scholarscraper.py)."""
    by_year: Dict[str, List] = {}
    for pub in pubs:
        y = pub.get("year", "") or "Undated"
        by_year.setdefault(y, []).append(pub)
    sorted_years = sorted(by_year.keys(), key=lambda y: y if y != "Undated" else "0000", reverse=True)

    author_name = ""
    if pubs:
        firsts = [p.get("authors","").split(",")[0].strip() for p in pubs[:15]
                  if p.get("authors")]
        if firsts:
            author_name = Counter(firsts).most_common(1)[0][0]
    total_citations = sum(p.get("citations", 0) for p in pubs)

    L = [r"""\documentclass[11pt,a4paper]{article}
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
\hypersetup{colorlinks=true,linkcolor=NavyBlue,urlcolor=NavyBlue,citecolor=NavyBlue,
  pdftitle={Publication List},pdfauthor={Generated by ScholarScraper}}
\titleformat{\section}{\Large\bfseries\color{NavyBlue}}{}{0em}{}
  [\vspace{-0.5em}\textcolor{NavyBlue}{\rule{\textwidth}{0.4pt}}]
\pagestyle{fancy}\fancyhf{}\renewcommand{\headrulewidth}{0pt}
\fancyfoot[C]{\small\textcolor{gray}{Page \thepage\ of \pageref{LastPage}}}
\begin{document}
\begin{center}
  {\LARGE\bfseries Publication List} \\[0.5em]"""]
    if scholar_id:
        L.append(r"  {\small\href{https://scholar.google.com/citations?user="
                 + scholar_id + r"}{Google Scholar Profile}} \\[0.3em]")
    L.append(r"  {\small\textcolor{gray}{Auto-generated \today{} ~$\cdot$~ "
             + tex_escape(str(len(pubs))) + r" publications ~$\cdot$~ "
             + tex_escape(str(total_citations)) + r" total citations}}")
    L.append(r"""\end{center}\vspace{1em}
{\small \textcolor{NavyBlue}{[\,DOI\,]}=publisher ~~$\cdot$~~
\textcolor{ForestGreen}{[\,PDF\,]}=open-access PDF ~~$\cdot$~~
\textcolor{Bittersweet}{[\,BIB\,]}=BibTeX ~~$\cdot$~~
\textcolor{gray}{[\,Scholar\,]}=Google Scholar}
\vspace{1em}""")

    pub_number = len(pubs)
    for year in sorted_years:
        L.append(r"\section*{" + tex_escape(year) + "}")
        yp = sorted(by_year[year], key=lambda p: p.get("citations", 0), reverse=True)
        sn = pub_number - len(yp) + 1
        L.append(r"\begin{enumerate}[label={\textbf{[\arabic*]}},start=" + str(sn) + r",leftmargin=2.5em]")
        for pub in yp:
            t = tex_escape(pub.get("title", "Untitled"))
            au = tex_escape(pub.get("authors", ""))
            ve = tex_escape(pub.get("venue", ""))
            if author_name:
                en = tex_escape(author_name)
                au = au.replace(en, r"\textbf{" + en + "}")
            iL = [r"  \item \textbf{" + t + "}"]
            if au: iL.append(r"  \\ " + au)
            if ve: iL.append(r"  \\ \textit{" + ve + "}")
            lnk = []
            if pub.get("doi"):
                lnk.append(r"\href{https://doi.org/" + pub["doi"] + r"}{\textcolor{NavyBlue}{\small[\,DOI\,]}}")
            if pub.get("pdf_file"):
                lnk.append(r"\href{./pdfs/" + os.path.basename(pub["pdf_file"]) + r"}{\textcolor{ForestGreen}{\small[\,PDF\,]}}")
            if pub.get("bib_txt_file"):
                lnk.append(r"\href{./bibtex/" + pub["bib_txt_file"] + r"}{\textcolor{Bittersweet}{\small[\,BIB\,]}}")
            if pub.get("scholar_link"):
                el = pub["scholar_link"].replace("%",r"\%").replace("#",r"\#").replace("&",r"\&")
                lnk.append(r"\href{" + el + r"}{\textcolor{gray}{\small[\,Scholar\,]}}")
            if pub.get("citations", 0) > 0:
                lnk.append(r"{\small\textcolor{gray}{" + str(pub["citations"]) + r" cit.}}")
            if lnk:
                iL.append(r"  \\ " + " ~~ ".join(lnk))
            L.append("\n".join(iL))
            L.append("")
        L.append(r"\end{enumerate}")
        L.append("")
        pub_number -= len(yp)
    L.append(r"\end{document}")
    tex_path.write_text("\n".join(L), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP Server
# ─────────────────────────────────────────────────────────────────────────────

scraper_thread: Optional[threading.Thread] = None


def _open_file(path: str):
    """Open a file or folder with the system's default application. Cross-platform."""
    system = platform.system()
    if system == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress server logs

    def _json_response(self, data: Any, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/defaults":
            home = str(Path.home() / "scholar_output")
            self._json_response({"output_dir": home})

        elif path == "/api/state":
            self._json_response(get_state_snapshot())

        elif path == "/api/publications":
            with state_lock:
                pubs = list(state["publications"])
            self._json_response(pubs)

        elif path == "/api/logs":
            with state_lock:
                logs = list(state["log"])
            self._json_response(logs)

        elif path == "/api/events":
            # SSE endpoint
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q: queue.Queue = queue.Queue(maxsize=200)
            sse_queues.append(q)
            try:
                while True:
                    try:
                        event_type, data = q.get(timeout=15)
                        msg = f"event: {event_type}\ndata: {data}\n\n"
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        # keepalive
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                if q in sse_queues:
                    sse_queues.remove(q)

        elif path == "/api/bibtex/all":
            with state_lock:
                pubs = list(state["publications"])
            all_bib = "\n\n".join(p.get("bibtex", "") for p in pubs if p.get("bibtex"))
            body = all_bib.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_error(404)

    def do_POST(self):
        global scraper_thread
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        if path == "/api/scrape":
            try:
                params = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                self._json_response({"error": "Invalid JSON"}, 400)
                return

            scholar_id = params.get("scholar_id", "").strip()
            if not scholar_id:
                self._json_response({"error": "scholar_id required"}, 400)
                return

            # Extract ID from full URL if pasted
            url_match = re.search(r"[?&]user=([a-zA-Z0-9_-]+)", scholar_id)
            if url_match:
                scholar_id = url_match.group(1)

            if scraper_thread and scraper_thread.is_alive():
                self._json_response({"error": "Scraper already running"}, 409)
                return

            # Reset state
            with state_lock:
                state["publications"] = []
                state["log"] = []
                state["error"] = ""
                state["stats"] = {"total": 0, "dois": 0, "pdfs": 0,
                                  "bibtex": 0, "total_citations": 0}

            output_dir = params.get("output_dir", "").strip()
            if not output_dir:
                output_dir = str(Path.home() / "scholar_output")
            cookie = params.get("cookie", "")
            email = params.get("unpaywall_email", "")
            skip_doi = params.get("skip_doi", False)
            include_pdf_dir = params.get("include_pdf_dir", "")

            scraper_thread = threading.Thread(
                target=run_scraper,
                args=(scholar_id, output_dir, cookie, email, skip_doi, include_pdf_dir),
                daemon=True,
            )
            scraper_thread.start()
            self._json_response({"ok": True, "scholar_id": scholar_id})

        elif path == "/api/reset":
            with state_lock:
                state["status"] = "idle"
                state["progress"] = 0
                state["progress_label"] = ""
                state["publications"] = []
                state["log"] = []
                state["error"] = ""
            self._json_response({"ok": True})

        elif path == "/api/open-pdf":
            with state_lock:
                out = state.get("output_dir", "")
            if not out:
                self._json_response({"error": "No output directory"}, 400)
                return
            pdf_path = Path(out) / "publications.pdf"
            if not pdf_path.exists():
                self._json_response({"error": f"PDF not found: {pdf_path}"}, 404)
                return
            try:
                _open_file(str(pdf_path))
                self._json_response({"ok": True, "path": str(pdf_path)})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/open-folder":
            with state_lock:
                out = state.get("output_dir", "")
            if not out:
                self._json_response({"error": "No output directory"}, 400)
                return
            folder = Path(out)
            if not folder.exists():
                self._json_response({"error": f"Folder not found: {folder}"}, 404)
                return
            try:
                _open_file(str(folder))
                self._json_response({"ok": True, "path": str(folder)})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard HTML/CSS/JS (embedded)
# ─────────────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ScholarScraper</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Source+Serif+4:ital,opsz,wght@0,8..60,300;0,8..60,500;0,8..60,700;1,8..60,400&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg:        #0e1117;
  --bg-card:   #161b22;
  --bg-input:  #0d1117;
  --bg-hover:  #1c2333;
  --border:    #30363d;
  --text:      #e6edf3;
  --text-dim:  #8b949e;
  --accent:    #58a6ff;
  --accent-bg: rgba(88,166,255,0.08);
  --green:     #3fb950;
  --green-bg:  rgba(63,185,80,0.1);
  --orange:    #d29922;
  --orange-bg: rgba(210,153,34,0.1);
  --red:       #f85149;
  --red-bg:    rgba(248,81,73,0.1);
  --font-body: 'DM Sans', system-ui, sans-serif;
  --font-head: 'Source Serif 4', Georgia, serif;
  --font-mono: 'JetBrains Mono', 'Consolas', monospace;
  --radius:    8px;
  --shadow:    0 1px 3px rgba(0,0,0,0.4), 0 6px 20px rgba(0,0,0,0.25);
}
[data-theme="light"] {
  --bg:        #f6f8fa;
  --bg-card:   #ffffff;
  --bg-input:  #f6f8fa;
  --bg-hover:  #f0f2f5;
  --border:    #d0d7de;
  --text:      #1f2328;
  --text-dim:  #656d76;
  --accent:    #0969da;
  --accent-bg: rgba(9,105,218,0.06);
  --green:     #1a7f37;
  --green-bg:  rgba(26,127,55,0.08);
  --orange:    #9a6700;
  --orange-bg: rgba(154,103,0,0.08);
  --red:       #cf222e;
  --red-bg:    rgba(207,34,46,0.06);
  --shadow:    0 1px 3px rgba(0,0,0,0.08), 0 4px 12px rgba(0,0,0,0.05);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 15px; }
body {
  font-family: var(--font-body);
  background: var(--bg);
  color: var(--text);
  line-height: 1.55;
  min-height: 100vh;
}
::selection { background: var(--accent); color: #fff; }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

/* ── Layout ── */
.shell {
  max-width: 1360px;
  margin: 0 auto;
  padding: 1.5rem 2rem 3rem;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 1.8rem;
  gap: 1rem;
  flex-wrap: wrap;
}
header h1 {
  font-family: var(--font-head);
  font-size: 1.75rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  background: linear-gradient(135deg, var(--accent), var(--green));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
header h1 span { font-weight: 300; opacity: 0.6; font-size: 0.8em; }

.theme-toggle {
  background: var(--bg-card);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 0.4rem 0.9rem;
  border-radius: 20px;
  cursor: pointer;
  font-size: 0.85rem;
  font-family: var(--font-body);
  transition: all 0.2s;
}
.theme-toggle:hover { border-color: var(--accent); background: var(--accent-bg); }

/* ── Cards ── */
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow: hidden;
}
.card-header {
  padding: 0.85rem 1.1rem;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
  font-size: 0.82rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-dim);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.card-body { padding: 1.1rem; }

/* ── Grid ── */
.grid-top { display: grid; grid-template-columns: 1fr 1fr; gap: 1.2rem; margin-bottom: 1.2rem; }
.grid-stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0.8rem; margin-bottom: 1.2rem; }
.grid-bottom { display: grid; grid-template-columns: 1fr 420px; gap: 1.2rem; }

@media (max-width: 1100px) {
  .grid-top, .grid-bottom { grid-template-columns: 1fr; }
  .grid-stats { grid-template-columns: repeat(3, 1fr); }
}

/* ── Config form ── */
.form-row { display: flex; gap: 0.6rem; margin-bottom: 0.7rem; align-items: end; }
.form-group { display: flex; flex-direction: column; gap: 0.25rem; flex: 1; }
.form-group label {
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.form-group input, .form-group select {
  background: var(--bg-input);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 0.55rem 0.75rem;
  border-radius: 6px;
  font-family: var(--font-mono);
  font-size: 0.82rem;
  outline: none;
  transition: border-color 0.15s;
}
.form-group input:focus { border-color: var(--accent); }
.form-group input::placeholder { color: var(--text-dim); opacity: 0.5; }

.btn {
  display: inline-flex; align-items: center; gap: 0.4rem;
  padding: 0.55rem 1.2rem;
  border-radius: 6px;
  border: 1px solid transparent;
  font-family: var(--font-body);
  font-size: 0.85rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.btn-primary {
  background: var(--accent);
  color: #fff;
}
.btn-primary:hover { filter: brightness(1.15); }
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-ghost {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text);
}
.btn-ghost:hover { background: var(--bg-hover); border-color: var(--accent); }
.btn-sm { padding: 0.35rem 0.75rem; font-size: 0.78rem; }

.checkbox-row {
  display: flex; align-items: center; gap: 0.5rem;
  font-size: 0.82rem; color: var(--text-dim);
}
.checkbox-row input[type="checkbox"] { accent-color: var(--accent); }

/* ── Progress ── */
.progress-wrap { margin-top: 0.8rem; }
.progress-bar-outer {
  background: var(--bg);
  border-radius: 4px;
  height: 8px;
  overflow: hidden;
}
.progress-bar-inner {
  height: 100%;
  background: linear-gradient(90deg, var(--accent), var(--green));
  border-radius: 4px;
  transition: width 0.4s ease;
  width: 0%;
}
.progress-label {
  font-size: 0.78rem;
  color: var(--text-dim);
  margin-top: 0.35rem;
  font-family: var(--font-mono);
}
.status-badge {
  display: inline-block;
  padding: 0.15rem 0.6rem;
  border-radius: 12px;
  font-size: 0.72rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.status-idle    { background: var(--bg); color: var(--text-dim); }
.status-running { background: var(--accent-bg); color: var(--accent); }
.status-done    { background: var(--green-bg); color: var(--green); }
.status-error   { background: var(--red-bg); color: var(--red); }

/* ── Stats ── */
.stat-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.9rem 1rem;
  text-align: center;
}
.stat-value {
  font-family: var(--font-head);
  font-size: 1.8rem;
  font-weight: 700;
  line-height: 1.1;
}
.stat-label {
  font-size: 0.72rem;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-top: 0.25rem;
}
.stat-accent { color: var(--accent); }
.stat-green  { color: var(--green); }
.stat-orange { color: var(--orange); }

/* ── Log ── */
.log-area {
  background: var(--bg);
  border-radius: 6px;
  padding: 0.75rem;
  height: 220px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 0.74rem;
  line-height: 1.65;
  color: var(--text-dim);
  border: 1px solid var(--border);
}
.log-area .log-line { white-space: pre-wrap; word-break: break-all; }
.log-area .log-line:has(✓), .log-area .log-ok { color: var(--green); }
.log-area .log-line:has(✗), .log-area .log-err { color: var(--red); }
.log-area .log-sep { color: var(--accent); font-weight: 600; }

/* ── Publication table ── */
.table-controls {
  display: flex; gap: 0.6rem; margin-bottom: 0.75rem; align-items: center; flex-wrap: wrap;
}
.search-input {
  flex: 1; min-width: 200px;
  background: var(--bg-input);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
  font-family: var(--font-body);
  font-size: 0.85rem;
  outline: none;
}
.search-input:focus { border-color: var(--accent); }

.pub-table-wrap {
  max-height: 520px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
}
table.pub-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.82rem;
}
table.pub-table thead th {
  position: sticky; top: 0;
  background: var(--bg-card);
  padding: 0.6rem 0.75rem;
  text-align: left;
  font-weight: 600;
  font-size: 0.73rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-dim);
  border-bottom: 2px solid var(--border);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}
table.pub-table thead th:hover { color: var(--accent); }
table.pub-table thead th .sort-arrow { font-size: 0.65rem; margin-left: 0.3rem; }
table.pub-table tbody tr {
  border-bottom: 1px solid var(--border);
  transition: background 0.1s;
  cursor: pointer;
}
table.pub-table tbody tr:hover { background: var(--bg-hover); }
table.pub-table tbody tr.selected { background: var(--accent-bg); }
table.pub-table td {
  padding: 0.55rem 0.75rem;
  vertical-align: top;
}
.td-title { font-weight: 500; max-width: 400px; }
.td-authors { color: var(--text-dim); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.td-venue { color: var(--text-dim); font-style: italic; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.td-year { text-align: center; font-family: var(--font-mono); font-size: 0.8rem; }
.td-cite { text-align: center; font-family: var(--font-mono); font-weight: 600; }
.td-links a {
  display: inline-block;
  padding: 0.15rem 0.45rem;
  border-radius: 4px;
  font-size: 0.7rem;
  font-weight: 600;
  text-decoration: none;
  margin-right: 0.25rem;
  transition: filter 0.1s;
}
.td-links a:hover { filter: brightness(1.3); }
.link-doi { background: var(--accent-bg); color: var(--accent); }
.link-pdf { background: var(--green-bg); color: var(--green); }
.link-scholar { background: var(--bg-hover); color: var(--text-dim); }

/* ── BibTeX panel ── */
.bib-panel { display: flex; flex-direction: column; height: 100%; }
.bib-preview {
  flex: 1;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.85rem;
  font-family: var(--font-mono);
  font-size: 0.78rem;
  line-height: 1.6;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text);
  min-height: 200px;
  max-height: 420px;
}
.bib-preview .bib-key { color: var(--accent); font-weight: 600; }
.bib-preview .bib-field { color: var(--orange); }
.bib-preview .bib-value { color: var(--green); }
.bib-actions {
  display: flex; gap: 0.5rem; margin-top: 0.7rem; flex-wrap: wrap;
}
.bib-placeholder {
  color: var(--text-dim);
  font-style: italic;
  text-align: center;
  padding: 3rem 1rem;
  font-family: var(--font-body);
}

/* ── Toast ── */
.toast {
  position: fixed; bottom: 1.5rem; right: 1.5rem;
  background: var(--bg-card);
  border: 1px solid var(--green);
  color: var(--green);
  padding: 0.65rem 1.1rem;
  border-radius: 8px;
  font-size: 0.85rem;
  font-weight: 500;
  box-shadow: var(--shadow);
  transform: translateY(100px);
  opacity: 0;
  transition: all 0.3s ease;
  z-index: 999;
}
.toast.show { transform: translateY(0); opacity: 1; }

/* ── Animations ── */
@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
.fade-in { animation: fadeIn 0.35s ease forwards; }
</style>
</head>
<body>
<div class="shell">

  <header>
    <h1>ScholarScraper <span>v0.1</span></h1>
    <button class="theme-toggle" onclick="toggleTheme()">◐ Theme</button>
  </header>

  <!-- Config + Log -->
  <div class="grid-top">
    <div class="card">
      <div class="card-header">⚙ Configuration</div>
      <div class="card-body">
        <div class="form-row">
          <div class="form-group" style="flex:2">
            <label>Scholar ID or Profile URL</label>
            <input type="text" id="scholarId" placeholder="r1tm9b4AAAAJ or full URL">
          </div>
          <div class="form-group">
            <label>Output Directory</label>
            <input type="text" id="outputDir" value="">
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>Institutional email, otherwise no PDF download</label>
            <input type="text" id="unpaywallEmail" placeholder="xxx@my-uni.edu">
          </div>
          <div class="form-group">
            <label>Scholar Cookie (optional)</label>
            <input type="text" id="cookie" placeholder="Paste cookie from browser DevTools (F12 → Network → Cookie header)">
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>Include PDFs from folder (optional), not fully working</label>
            <input type="text" id="includePdfDir" placeholder="/home/user/Papers or C:\Papers — matched by title">
          </div>
        </div>
        <div class="form-row" style="justify-content: space-between; align-items: center;">
          <label class="checkbox-row">
            <input type="checkbox" id="skipDoi"> Skip DOI resolution (fast mode)
          </label>
          <div style="display:flex; gap:0.5rem;">
            <button class="btn btn-ghost btn-sm" onclick="resetAll()">Reset</button>
            <button class="btn btn-ghost btn-sm" id="btnOpenPdf" onclick="openPdf()" style="display:none">📄 Open PDF</button>
            <button class="btn btn-ghost btn-sm" id="btnOpenFolder" onclick="openFolder()" style="display:none">📂 Open Folder</button>
            <button class="btn btn-primary" id="btnScrape" onclick="startScrape()">▶ Start Scraping</button>
          </div>
        </div>
        <div class="progress-wrap">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.3rem;">
            <span id="statusBadge" class="status-badge status-idle">IDLE</span>
            <span id="progressPct" style="font-family:var(--font-mono);font-size:0.8rem;color:var(--text-dim);">0%</span>
          </div>
          <div class="progress-bar-outer"><div class="progress-bar-inner" id="progressBar"></div></div>
          <div class="progress-label" id="progressLabel">&nbsp;</div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">📋 Live Log</div>
      <div class="card-body" style="padding:0.6rem;">
        <div class="log-area" id="logArea"></div>
      </div>
    </div>
  </div>

  <!-- Stats -->
  <div class="grid-stats" id="statsGrid">
    <div class="stat-card"><div class="stat-value stat-accent" id="statTotal">—</div><div class="stat-label">Publications</div></div>
    <div class="stat-card"><div class="stat-value stat-accent" id="statDois">—</div><div class="stat-label">DOIs Found</div></div>
    <div class="stat-card"><div class="stat-value stat-green" id="statPdfs">—</div><div class="stat-label">PDFs Downloaded</div></div>
    <div class="stat-card"><div class="stat-value stat-orange" id="statBibtex">—</div><div class="stat-label">BibTeX Entries</div></div>
    <div class="stat-card"><div class="stat-value" id="statCitations">—</div><div class="stat-label">Total Citations</div></div>
  </div>

  <!-- Table + BibTeX panel -->
  <div class="grid-bottom">
    <div class="card">
      <div class="card-header">📚 Publications <span id="pubCountLabel" style="margin-left:auto;font-weight:400;font-size:0.78rem;"></span></div>
      <div class="card-body">
        <div class="table-controls">
          <input type="text" class="search-input" id="searchInput" placeholder="Search titles, authors, venues..." oninput="renderTable()">
          <select id="sortSelect" onchange="renderTable()" style="background:var(--bg-input);border:1px solid var(--border);color:var(--text);padding:0.45rem 0.6rem;border-radius:6px;font-size:0.82rem;">
            <option value="year-desc">Year ↓</option>
            <option value="year-asc">Year ↑</option>
            <option value="cite-desc">Citations ↓</option>
            <option value="cite-asc">Citations ↑</option>
            <option value="title-asc">Title A-Z</option>
          </select>
          <button class="btn btn-ghost btn-sm" onclick="copyAllBibtex()">📋 Copy All BibTeX</button>
        </div>
        <div class="pub-table-wrap">
          <table class="pub-table">
            <thead>
              <tr>
                <th style="width:40px">#</th>
                <th>Title</th>
                <th>Authors</th>
                <th>Venue</th>
                <th style="width:55px">Year</th>
                <th style="width:50px">Cite</th>
                <th style="width:100px">Links</th>
              </tr>
            </thead>
            <tbody id="pubTableBody">
              <tr><td colspan="7" style="text-align:center;padding:2rem;color:var(--text-dim);font-style:italic;">Enter a Scholar ID and click Start Scraping</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">📄 BibTeX Preview</div>
      <div class="card-body">
        <div class="bib-panel">
          <div class="bib-preview" id="bibPreview">
            <div class="bib-placeholder">Click a publication to preview its BibTeX entry</div>
          </div>
          <div class="bib-actions">
            <button class="btn btn-ghost btn-sm" id="btnCopyBib" onclick="copySelectedBibtex()" disabled>📋 Copy Entry</button>
            <button class="btn btn-ghost btn-sm" id="btnCopyKey" onclick="copySelectedKey()" disabled>🔑 Copy Key</button>
          </div>
        </div>
      </div>
    </div>
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
// ── State ──
let publications = [];
let selectedIdx = -1;

// ── Theme ──
function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}
(function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  else if (window.matchMedia('(prefers-color-scheme: light)').matches)
    document.documentElement.setAttribute('data-theme', 'light');
})();

// ── Toast ──
function showToast(msg, duration = 2000) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), duration);
}

// ── SSE ──
let eventSource = null;
function connectSSE() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/events');
  eventSource.addEventListener('state', e => {
    const s = JSON.parse(e.data);
    updateUI(s);
  });
  eventSource.addEventListener('log', e => {
    appendLog(e.data);
  });
  eventSource.onerror = () => {
    setTimeout(connectSSE, 3000);
  };
}
connectSSE();

// ── UI Updates ──
function updateUI(s) {
  // Status badge
  const badge = document.getElementById('statusBadge');
  const running = ['scraping','resolving','bibtex','latex'].includes(s.status);
  badge.textContent = s.status.toUpperCase();
  badge.className = 'status-badge ' + (
    s.status === 'idle' ? 'status-idle' :
    s.status === 'done' ? 'status-done' :
    s.status === 'error' ? 'status-error' : 'status-running'
  );

  // Progress
  document.getElementById('progressBar').style.width = s.progress + '%';
  document.getElementById('progressPct').textContent = s.progress + '%';
  document.getElementById('progressLabel').textContent = s.progress_label || '\u00a0';

  // Button
  document.getElementById('btnScrape').disabled = running;
  document.getElementById('btnScrape').textContent = running ? '⏳ Running...' : '▶ Start Scraping';

  // Show/hide open buttons
  const isDone = s.status === 'done';
  document.getElementById('btnOpenPdf').style.display = isDone ? '' : 'none';
  document.getElementById('btnOpenFolder').style.display = isDone ? '' : 'none';

  // Stats
  document.getElementById('statTotal').textContent = s.stats.total || '—';
  document.getElementById('statDois').textContent = s.stats.dois || '—';
  document.getElementById('statPdfs').textContent = s.stats.pdfs || '—';
  document.getElementById('statBibtex').textContent = s.stats.bibtex || '—';
  document.getElementById('statCitations').textContent = s.stats.total_citations ? s.stats.total_citations.toLocaleString() : '—';

  // Refresh table when done or publications arrive
  if (s.pub_count > 0 && (s.pub_count !== publications.length || s.status === 'done')) {
    fetchPublications();
  }
}

function appendLog(msg) {
  const area = document.getElementById('logArea');
  const div = document.createElement('div');
  div.className = 'log-line';
  if (msg.includes('═══')) div.classList.add('log-sep');
  else if (msg.includes('✓')) div.classList.add('log-ok');
  else if (msg.includes('✗') || msg.includes('ERROR')) div.classList.add('log-err');
  div.textContent = msg;
  area.appendChild(div);
  area.scrollTop = area.scrollHeight;
}

async function fetchPublications() {
  try {
    const res = await fetch('/api/publications');
    publications = await res.json();
    renderTable();
  } catch (e) {}
}

// ── Table rendering ──
function renderTable() {
  const query = document.getElementById('searchInput').value.toLowerCase();
  const sort = document.getElementById('sortSelect').value;

  let filtered = publications;
  if (query) {
    filtered = publications.filter(p =>
      (p.title || '').toLowerCase().includes(query) ||
      (p.authors || '').toLowerCase().includes(query) ||
      (p.venue || '').toLowerCase().includes(query) ||
      (p.year || '').includes(query)
    );
  }

  // Sort
  filtered = [...filtered];
  switch (sort) {
    case 'year-desc': filtered.sort((a,b) => (b.year||'0').localeCompare(a.year||'0')); break;
    case 'year-asc':  filtered.sort((a,b) => (a.year||'0').localeCompare(b.year||'0')); break;
    case 'cite-desc': filtered.sort((a,b) => (b.citations||0) - (a.citations||0)); break;
    case 'cite-asc':  filtered.sort((a,b) => (a.citations||0) - (b.citations||0)); break;
    case 'title-asc': filtered.sort((a,b) => (a.title||'').localeCompare(b.title||'')); break;
  }

  document.getElementById('pubCountLabel').textContent = filtered.length + ' shown';

  const tbody = document.getElementById('pubTableBody');
  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2rem;color:var(--text-dim);font-style:italic;">No publications found</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.map((p, i) => {
    const globalIdx = publications.indexOf(p);
    const sel = globalIdx === selectedIdx ? ' selected' : '';
    let links = '';
    if (p.doi) links += `<a href="https://doi.org/${p.doi}" target="_blank" class="link-doi" onclick="event.stopPropagation()">DOI</a>`;
    if (p.pdf_file) links += `<a href="#" class="link-pdf" onclick="event.stopPropagation()">PDF</a>`;
    if (p.scholar_link) links += `<a href="${p.scholar_link}" target="_blank" class="link-scholar" onclick="event.stopPropagation()">GS</a>`;
    return `<tr class="${sel}" onclick="selectPub(${globalIdx})">
      <td class="td-year" style="color:var(--text-dim)">${i+1}</td>
      <td class="td-title">${escHtml(p.title||'')}</td>
      <td class="td-authors">${escHtml(p.authors||'')}</td>
      <td class="td-venue">${escHtml(p.venue||'')}</td>
      <td class="td-year">${p.year||''}</td>
      <td class="td-cite">${p.citations||''}</td>
      <td class="td-links">${links}</td>
    </tr>`;
  }).join('');
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── BibTeX preview ──
function selectPub(idx) {
  selectedIdx = idx;
  const pub = publications[idx];
  const preview = document.getElementById('bibPreview');

  if (!pub || !pub.bibtex) {
    preview.innerHTML = '<div class="bib-placeholder">No BibTeX available for this entry</div>';
    document.getElementById('btnCopyBib').disabled = true;
    document.getElementById('btnCopyKey').disabled = true;
  } else {
    // Syntax highlight the bibtex
    let bib = escHtml(pub.bibtex);
    // Highlight entry type + key
    bib = bib.replace(/^(@\w+)\{([^,]+),/m, '<span class="bib-key">$1</span>{<span class="bib-key">$2</span>,');
    // Highlight field names
    bib = bib.replace(/^(\s+)(\w+)(\s*=)/gm, '$1<span class="bib-field">$2</span>$3');
    // Highlight values in braces
    bib = bib.replace(/\{([^}]*)\}/g, '{<span class="bib-value">$1</span>}');
    preview.innerHTML = bib;
    document.getElementById('btnCopyBib').disabled = false;
    document.getElementById('btnCopyKey').disabled = false;
  }

  renderTable(); // re-render to update selection highlight
}

// ── Clipboard ──
function copySelectedBibtex() {
  if (selectedIdx < 0 || !publications[selectedIdx]) return;
  const bib = publications[selectedIdx].bibtex || '';
  navigator.clipboard.writeText(bib).then(() => showToast('✓ BibTeX copied'));
}

function copySelectedKey() {
  if (selectedIdx < 0 || !publications[selectedIdx]) return;
  const key = publications[selectedIdx].cite_key || '';
  navigator.clipboard.writeText(key).then(() => showToast('✓ Citation key copied: ' + key));
}

async function copyAllBibtex() {
  if (!publications.length) return;
  try {
    const res = await fetch('/api/bibtex/all');
    const text = await res.text();
    await navigator.clipboard.writeText(text);
    showToast('✓ All BibTeX entries copied (' + publications.length + ')');
  } catch (e) {
    showToast('✗ Copy failed');
  }
}

// ── Actions ──
async function startScrape() {
  const scholarId = document.getElementById('scholarId').value.trim();
  if (!scholarId) { showToast('Enter a Scholar ID or URL'); return; }

  document.getElementById('logArea').innerHTML = '';

  const body = {
    scholar_id: scholarId,
    output_dir: document.getElementById('outputDir').value.trim() || './scholar_output',
    cookie: document.getElementById('cookie').value.trim(),
    unpaywall_email: document.getElementById('unpaywallEmail').value.trim(),
    skip_doi: document.getElementById('skipDoi').checked,
    include_pdf_dir: document.getElementById('includePdfDir').value.trim(),
  };

  try {
    const res = await fetch('/api/scrape', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.error) showToast('✗ ' + data.error);
  } catch (e) {
    showToast('✗ Failed to start');
  }
}

async function resetAll() {
  await fetch('/api/reset', { method: 'POST' });
  publications = [];
  selectedIdx = -1;
  document.getElementById('logArea').innerHTML = '';
  document.getElementById('bibPreview').innerHTML = '<div class="bib-placeholder">Click a publication to preview its BibTeX entry</div>';
  document.getElementById('btnCopyBib').disabled = true;
  document.getElementById('btnCopyKey').disabled = true;
  document.getElementById('btnOpenPdf').style.display = 'none';
  document.getElementById('btnOpenFolder').style.display = 'none';
  renderTable();
  showToast('Reset complete');
}

async function openPdf() {
  try {
    const res = await fetch('/api/open-pdf', { method: 'POST' });
    const data = await res.json();
    if (data.error) showToast('✗ ' + data.error);
    else showToast('✓ Opening PDF...');
  } catch (e) { showToast('✗ Failed to open PDF'); }
}

async function openFolder() {
  try {
    const res = await fetch('/api/open-folder', { method: 'POST' });
    const data = await res.json();
    if (data.error) showToast('✗ ' + data.error);
    else showToast('✓ Opening folder...');
  } catch (e) { showToast('✗ Failed to open folder'); }
}

// ── Init ──
fetch('/api/defaults').then(r => r.json()).then(d => {
  if (d.output_dir && !document.getElementById('outputDir').value)
    document.getElementById('outputDir').value = d.output_dir;
}).catch(() => {});
fetch('/api/state').then(r => r.json()).then(updateUI).catch(() => {});
fetch('/api/publications').then(r => r.json()).then(p => { publications = p; renderTable(); }).catch(() => {});
fetch('/api/logs').then(r => r.json()).then(logs => {
  logs.forEach(l => appendLog(l));
}).catch(() => {});
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="ScholarScraper Web GUI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to run on (default: {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser")
    args = parser.parse_args()

    port = args.port

    class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = ThreadedTCPServer(("", port), Handler)

    url = f"http://localhost:{port}"
    print(f"\n  ScholarScraper GUI v{VERSION}")
    print(f"  Running at: {url}")
    print(f"  Press Ctrl+C to stop\n")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
