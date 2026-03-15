#!/usr/bin/env bash
#===============================================================================
#  scholar_publications.sh  —  v0.1.0
#
#  Scrape a Google Scholar profile, extract publications, attempt to download
#  open-access PDFs, and compile a clickable LaTeX publication list into a PDF.
#
#  Usage:
#      ./scholar_publications.sh <GOOGLE_SCHOLAR_USER_ID> [OUTPUT_DIR]
#
#  Example:
#      ./scholar_publications.sh "bILmewYAAAAJ"               # uses ./scholar_output
#      ./scholar_publications.sh "bILmewYAAAAJ" ~/my_pubs     # custom output dir
#
#  Dependencies (auto-checked):
#      curl, python3, pdflatex (texlive), jq
#
#  Optional:
#      pip install beautifulsoup4   ← much more reliable HTML parsing
#
#  Notes:
#  - Google Scholar may rate-limit or block automated requests. The script
#    uses polite delays and a browser-like User-Agent, but heavy use may
#    still trigger CAPTCHAs. If that happens, wait a while and retry.
#  - PDF downloads rely on Unpaywall (free, needs an email) and open-access
#    links. Not every paper will have a freely available PDF.
#  - Set the environment variable UNPAYWALL_EMAIL to your email address for
#    the Unpaywall API. If unset, the script skips Unpaywall lookups.
#  - Set DEBUG=1 to dump diagnostic info about the HTML pages fetched.
#===============================================================================

set -euo pipefail

#-------------------------------  Configuration  -------------------------------
SCHOLAR_ID="${1:-}"
OUTPUT_DIR="${2:-./scholar_output}"
PDF_DIR="${OUTPUT_DIR}/pdfs"
BIBTEX_DIR="${OUTPUT_DIR}/bibtex"
DATA_DIR="${OUTPUT_DIR}/data"
TEX_FILE="${OUTPUT_DIR}/publications.tex"
PDF_OUTPUT="${OUTPUT_DIR}/publications.pdf"
UNPAYWALL_EMAIL="${UNPAYWALL_EMAIL:-}"        # set to enable Unpaywall API
SKIP_DOI="${SKIP_DOI:-0}"                     # set to 1 to skip DOI/PDF resolution
DEBUG="${DEBUG:-0}"                            # set to 1 for diagnostic output
DELAY_SECONDS=3                               # politeness delay between requests
USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MAX_PAGES=10                                  # max Scholar profile pages to fetch
PAGESIZE=100                                  # articles per Scholar page (max 100)

#-------------------------------  Helpers  -------------------------------------

red()    { printf '\033[1;31m%s\033[0m\n' "$*"; }
green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
blue()   { printf '\033[1;34m%s\033[0m\n' "$*"; }

die() { red "ERROR: $*" >&2; exit 1; }

check_deps() {
    local missing=()
    for cmd in curl python3 pdflatex jq; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if (( ${#missing[@]} )); then
        die "Missing dependencies: ${missing[*]}
Install them, e.g.:
  sudo apt-get install curl python3 jq texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended latexmk"
    fi
}

usage() {
    cat <<'EOF'
Usage: ./scholar_publications.sh <GOOGLE_SCHOLAR_USER_ID> [OUTPUT_DIR]

  GOOGLE_SCHOLAR_USER_ID   The "user" parameter from your Google Scholar URL.
                           e.g. https://scholar.google.com/citations?user=bILmewYAAAAJ
                           →  bILmewYAAAAJ

  OUTPUT_DIR               (optional) Output directory. Default: ./scholar_output

Environment variables:
  UNPAYWALL_EMAIL          Your email for the Unpaywall API (enables PDF lookup
                           via DOI). Example: export UNPAYWALL_EMAIL="me@uni.edu"
  SCHOLAR_COOKIE           Browser cookie string for Scholar (bypasses consent pages).
  SKIP_DOI=1               Skip DOI resolution & PDF download (fast mode, LaTeX only).
  DEBUG=1                  Enable diagnostic output (dumps HTML snippets).
EOF
    exit 0
}

#-----------------------------  Dependency check  ------------------------------

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage
[[ -z "$SCHOLAR_ID" ]] && usage
check_deps

mkdir -p "$PDF_DIR" "$BIBTEX_DIR" "$DATA_DIR"

#=============================  STEP 1: SCRAPE  ================================
blue "╔══════════════════════════════════════════════════════════════╗"
blue "║  Step 1 — Fetching publications from Google Scholar         ║"
blue "╚══════════════════════════════════════════════════════════════╝"

# ---- Python parser: tries BeautifulSoup first, falls back to regex ----
cat > "${DATA_DIR}/_parse_scholar.py" << 'PYEOF'
#!/usr/bin/env python3
"""
Parse Google Scholar profile HTML pages and emit JSON.
Uses BeautifulSoup if available, otherwise falls back to regex.
"""

import html as html_mod
import json
import re
import sys
import os

DEBUG = os.environ.get('DEBUG', '0') == '1'

def dbg(msg):
    if DEBUG:
        print(f"  [DEBUG] {msg}", file=sys.stderr, flush=True)

def clean(text):
    """Unescape HTML entities, strip tags, and normalise whitespace."""
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', '', str(text))
    text = html_mod.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ---------- BeautifulSoup-based parser ----------
def parse_with_bs4(html_content):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, 'html.parser')
    articles = []

    rows = soup.select('tr.gsc_a_tr')
    dbg(f"BS4: found {len(rows)} tr.gsc_a_tr rows")

    if not rows:
        table = soup.find('table', id='gsc_a_t')
        if table:
            rows = table.find_all('tr')
            dbg(f"BS4: found {len(rows)} rows in #gsc_a_t table")

    # Filter out hidden header rows
    rows = [r for r in rows if r.get('aria-hidden') != 'true']
    dbg(f"BS4: {len(rows)} visible rows after filtering aria-hidden")

    for row in rows:
        article = {}

        # Title
        td_title = row.find('td', class_='gsc_a_t')
        if td_title:
            link = td_title.find('a')
            if link:
                article['title'] = clean(link.get_text())
                href = link.get('href', '')
                if href.startswith('/'):
                    href = 'https://scholar.google.com' + href
                elif not href.startswith('http'):
                    href = 'https://scholar.google.com/' + href
                article['scholar_link'] = href
        else:
            link = row.find('a', class_='gsc_a_at')
            if link:
                article['title'] = clean(link.get_text())
                href = link.get('href', '')
                if href.startswith('/'):
                    href = 'https://scholar.google.com' + href
                article['scholar_link'] = href

        if 'title' not in article or not article['title']:
            continue

        # Authors and venue
        container = td_title if td_title else row
        grays = container.find_all('div', class_='gs_gray')
        if len(grays) >= 1:
            article['authors'] = clean(grays[0].get_text())
        if len(grays) >= 2:
            article['venue'] = clean(grays[1].get_text())

        # Year
        td_year = row.find('td', class_='gsc_a_y')
        if td_year:
            year_text = clean(td_year.get_text())
            year_m = re.search(r'(\d{4})', year_text)
            article['year'] = year_m.group(1) if year_m else ''
        else:
            year_span = row.find('span', class_=re.compile(r'gsc_a_h'))
            if year_span:
                year_m = re.search(r'(\d{4})', year_span.get_text())
                article['year'] = year_m.group(1) if year_m else ''
            else:
                article['year'] = ''

        # Citations
        td_cite = row.find('td', class_='gsc_a_c')
        cite_text = ''
        if td_cite:
            cite_link = td_cite.find('a')
            cite_text = clean(cite_link.get_text()) if cite_link else clean(td_cite.get_text())
        else:
            cite_link = row.find('a', class_=re.compile(r'gsc_a_ac'))
            if cite_link:
                cite_text = clean(cite_link.get_text())
        cite_m = re.search(r'(\d+)', cite_text)
        article['citations'] = int(cite_m.group(1)) if cite_m else 0

        articles.append(article)

    return articles

# ---------- Regex-based fallback parser ----------
def parse_with_regex(html_content):
    articles = []

    # Strategy 1: <tr class="gsc_a_tr">...</tr> (class anywhere in attributes)
    rows = re.findall(r'<tr\s[^>]*?gsc_a_tr[^>]*?>(.*?)</tr>', html_content, re.DOTALL)
    dbg(f"Regex strategy 1 (gsc_a_tr anywhere): {len(rows)} rows")

    # Strategy 2: inside #gsc_a_t table
    if not rows:
        table_m = re.search(r'<table[^>]*?id=["\']?gsc_a_t["\']?[^>]*?>(.*?)</table>', html_content, re.DOTALL)
        if table_m:
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_m.group(1), re.DOTALL)
            dbg(f"Regex strategy 2 (inside #gsc_a_t): {len(rows)} rows")

    # Strategy 3: just find every <a> with gsc_a_at anywhere
    if not rows:
        links = list(re.finditer(
            r'<a\s[^>]*?gsc_a_at[^>]*?>(.*?)</a>',
            html_content, re.DOTALL
        ))
        dbg(f"Regex strategy 3 (bare gsc_a_at links): {len(links)} links")

        for m in links:
            full_tag = html_content[m.start()-200:m.end()+50]
            href_m = re.search(r'href="([^"]*)"', full_tag)
            href = href_m.group(1) if href_m else ''
            title = clean(m.group(1))
            if title:
                if href.startswith('/'):
                    href = 'https://scholar.google.com' + html_mod.unescape(href)
                articles.append({
                    'title': title,
                    'scholar_link': href,
                    'authors': '',
                    'venue': '',
                    'year': '',
                    'citations': 0,
                })
        if articles:
            # Try to enrich with surrounding context (authors, year)
            _enrich_from_context(html_content, articles)
            return articles

    for row in rows:
        article = {}

        # Title: flexible — class and href can be in any order
        title_m = re.search(
            r'<a\s[^>]*?gsc_a_at[^>]*?>(.*?)</a>',
            row, re.DOTALL
        )
        if title_m:
            full_a = row[title_m.start():title_m.end()+5]
            # Re-extract from the broader match to get href
            a_tag_m = re.search(r'<a\s([^>]*?)>', row[title_m.start():])
            href = ''
            if a_tag_m:
                href_m = re.search(r'href="([^"]*)"', a_tag_m.group(1))
                if href_m:
                    href = html_mod.unescape(href_m.group(1))
            article['title'] = clean(title_m.group(1))
            if href.startswith('/'):
                href = 'https://scholar.google.com' + href
            article['scholar_link'] = href
        else:
            continue

        # Authors + venue
        grays = re.findall(r'<div\s[^>]*?gs_gray[^>]*?>(.*?)</div>', row, re.DOTALL)
        if len(grays) >= 1:
            article['authors'] = clean(grays[0])
        if len(grays) >= 2:
            article['venue'] = clean(grays[1])

        # Year
        year_m = (
            re.search(r'class="gsc_a_y"[^>]*>.*?(\d{4})', row, re.DOTALL) or
            re.search(r'gsc_a_h[^>]*>(\d{4})', row, re.DOTALL) or
            re.search(r'>(\d{4})</', row)
        )
        article['year'] = year_m.group(1) if year_m else ''

        # Citations
        cite_m = (
            re.search(r'gsc_a_ac[^>]*>(\d+)<', row) or
            re.search(r'class="gsc_a_c"[^>]*>.*?(\d+)', row, re.DOTALL)
        )
        article['citations'] = int(cite_m.group(1)) if cite_m else 0

        articles.append(article)

    return articles

def _enrich_from_context(html_content, articles):
    """Try to find authors/venue/year near each title in the HTML."""
    for art in articles:
        title = art.get('title', '')
        if not title:
            continue
        # Find the title in HTML and look at surrounding context
        esc_title = re.escape(title[:40])
        m = re.search(esc_title, html_content)
        if not m:
            continue
        # Look at the next ~1000 chars for gs_gray divs and year
        context = html_content[m.end():m.end()+1000]
        grays = re.findall(r'<div\s[^>]*?gs_gray[^>]*?>(.*?)</div>', context, re.DOTALL)
        if len(grays) >= 1 and not art.get('authors'):
            art['authors'] = clean(grays[0])
        if len(grays) >= 2 and not art.get('venue'):
            art['venue'] = clean(grays[1])
        year_m = re.search(r'>(\d{4})</', context)
        if year_m and not art.get('year'):
            art['year'] = year_m.group(1)

# ---------- Diagnostic ----------
def check_page_health(html_content, page_num):
    lower = html_content.lower()

    if 'captcha' in lower or 'recaptcha' in lower or 'unusual traffic' in lower:
        print(f"  ⚠ Page {page_num}: CAPTCHA / rate-limit detected!", file=sys.stderr)
        return 'captcha'
    if 'consent.google.com' in lower or 'before you continue' in lower:
        print(f"  ⚠ Page {page_num}: Google consent page detected!", file=sys.stderr)
        return 'consent'
    if len(html_content.strip()) < 500:
        print(f"  ⚠ Page {page_num}: Very short response ({len(html_content)} bytes)", file=sys.stderr)
        return 'empty'
    return 'ok'

# ---------- Main ----------
def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else '.'
    all_articles = []
    total_pages = 0

    use_bs4 = False
    try:
        from bs4 import BeautifulSoup
        use_bs4 = True
        dbg("Using BeautifulSoup parser (recommended)")
    except ImportError:
        dbg("BeautifulSoup not available — using regex parser")
        print("  ℹ  Tip: pip install beautifulsoup4  (more reliable parsing)", file=sys.stderr)

    page = 0
    while True:
        fpath = os.path.join(data_dir, f'scholar_page_{page}.html')
        if not os.path.exists(fpath):
            break
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        total_pages += 1
        status = check_page_health(content, page)
        if status in ('captcha', 'consent'):
            page += 1
            continue

        if DEBUG:
            classes = set(re.findall(r'class="([^"]*)"', content))
            scholar_classes = sorted([c for c in classes if 'gsc' in c or 'gs_' in c])
            dbg(f"Page {page}: {len(content)} bytes")
            dbg(f"Page {page}: Scholar classes found: {scholar_classes}")
            # Count occurrences of key markers
            dbg(f"Page {page}: 'gsc_a_tr' occurs {content.count('gsc_a_tr')} times")
            dbg(f"Page {page}: 'gsc_a_at' occurs {content.count('gsc_a_at')} times")
            dbg(f"Page {page}: 'gsc_a_t' occurs {content.count('gsc_a_t')} times")
            dbg(f"Page {page}: 'gs_gray' occurs {content.count('gs_gray')} times")
            # Show first row
            first_tr = re.search(r'<tr[^>]*gsc_a_tr[^>]*>(.*?)</tr>', content, re.DOTALL)
            if first_tr:
                dbg(f"Page {page}: First row (500c): {first_tr.group(0)[:500]}")
            else:
                # Show context around gsc_a_at
                at_pos = content.find('gsc_a_at')
                if at_pos >= 0:
                    snippet = content[max(0,at_pos-300):at_pos+400]
                    dbg(f"Page {page}: Context around first 'gsc_a_at':\n{snippet}")

        # Parse
        if use_bs4:
            articles = parse_with_bs4(content)
        else:
            articles = parse_with_regex(content)

        dbg(f"Page {page}: extracted {len(articles)} articles")

        if not articles and page > 0:
            break
        all_articles.extend(articles)
        page += 1

    if not all_articles and total_pages > 0:
        print("\n  ✗ Parser found 0 articles across all pages.", file=sys.stderr)
        print("  Possible causes:", file=sys.stderr)
        print("    1. Google Scholar served a consent/cookie page", file=sys.stderr)
        print("    2. Google Scholar served a CAPTCHA", file=sys.stderr)
        print("    3. The HTML structure has changed", file=sys.stderr)
        print("\n  TROUBLESHOOTING:", file=sys.stderr)
        print("    • Run with DEBUG=1 to see HTML details:", file=sys.stderr)
        print(f"        DEBUG=1 ./scholar_publications.sh ...", file=sys.stderr)
        print("    • Inspect the raw HTML:", file=sys.stderr)
        print(f"        head -200 {data_dir}/scholar_page_0.html", file=sys.stderr)
        print("    • Install beautifulsoup4:", file=sys.stderr)
        print("        pip install beautifulsoup4", file=sys.stderr)
        print("    • If consent page, export cookies:", file=sys.stderr)
        print("        export SCHOLAR_COOKIE='NID=...'", file=sys.stderr)

    # Deduplicate
    seen = set()
    unique = []
    for a in all_articles:
        key = a.get('title', '').lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(a)

    unique.sort(key=lambda a: (a.get('year', '0'), a.get('citations', 0)), reverse=True)
    json.dump(unique, sys.stdout, ensure_ascii=False, indent=2)

if __name__ == '__main__':
    main()
PYEOF

# Fetch Scholar profile pages
page_idx=0
start=0
while (( page_idx < MAX_PAGES )); do
    url="https://scholar.google.com/citations?user=${SCHOLAR_ID}&cstart=${start}&pagesize=${PAGESIZE}&sortby=pubdate&hl=en"
    outfile="${DATA_DIR}/scholar_page_${page_idx}.html"

    yellow "  Fetching page $((page_idx+1)) (start=${start}) ..."

    curl_args=(
        -s -L
        -o "$outfile"
        -w '%{http_code}'
        -H "User-Agent: ${USER_AGENT}"
        -H "Accept-Language: en-US,en;q=0.9"
        -H "Accept: text/html,application/xhtml+xml"
    )
    if [[ -n "${SCHOLAR_COOKIE:-}" ]]; then
        curl_args+=(-H "Cookie: ${SCHOLAR_COOKIE}")
    fi

    http_code=$(curl "${curl_args[@]}" "$url" 2>/dev/null || echo "000")

    if [[ "$http_code" != "200" ]]; then
        yellow "  Got HTTP ${http_code} — stopping pagination."
        rm -f "$outfile"
        break
    fi

    # Detect problems in the response
    if grep -qi 'captcha\|unusual traffic\|recaptcha' "$outfile" 2>/dev/null; then
        red "  ✗ CAPTCHA detected! Google is rate-limiting requests."
        red "    Wait 10–30 minutes and try again, or use a different network."
        rm -f "$outfile"
        break
    fi
    if grep -qi 'consent.google\|before you continue' "$outfile" 2>/dev/null; then
        red "  ✗ Google consent page detected!"
        yellow "    Workaround: export your browser cookies for scholar.google.com:"
        yellow "      1. Open your Scholar profile in Chrome/Firefox"
        yellow "      2. Open DevTools (F12) → Network tab → reload page"
        yellow "      3. Click the first request → Headers → copy the Cookie value"
        yellow "      4. export SCHOLAR_COOKIE='NID=...; GSP=...'"
        yellow "      5. Re-run this script"
        rm -f "$outfile"
        break
    fi

    # Check for any article content at all
    if ! grep -q 'gsc_a_at\|gsc_a_t\|gsc_a_tr' "$outfile" 2>/dev/null; then
        yellow "  No article markers found on page $((page_idx+1)) — stopping."
        [[ "$DEBUG" == "1" ]] && { echo "  [DEBUG] First 80 lines of response:"; head -80 "$outfile" >&2; }
        rm -f "$outfile"
        break
    fi

    # Debug: quick stats
    if [[ "$DEBUG" == "1" ]]; then
        at_count=$(grep -o 'gsc_a_at' "$outfile" | wc -l)
        tr_count=$(grep -o 'gsc_a_tr' "$outfile" | wc -l)
        echo "  [DEBUG] Page ${page_idx}: gsc_a_at=${at_count}, gsc_a_tr=${tr_count}" >&2
    fi

    page_idx=$((page_idx + 1))
    start=$((start + PAGESIZE))
    sleep "$DELAY_SECONDS"
done

green "  Fetched $page_idx page(s)."

if (( page_idx == 0 )); then
    die "Could not fetch any usable pages from Google Scholar. Check your network and Scholar ID."
fi

# Parse HTML into JSON
yellow "  Parsing publications ..."
DEBUG=$DEBUG python3 "${DATA_DIR}/_parse_scholar.py" "$DATA_DIR" > "${DATA_DIR}/publications.json"
PUB_COUNT=$(jq 'length' "${DATA_DIR}/publications.json")
green "  Found ${PUB_COUNT} unique publication(s)."

if (( PUB_COUNT == 0 )); then
    echo ""
    red "  No publications could be extracted from the HTML."
    yellow "  Run with DEBUG=1 for detailed diagnostics:"
    yellow "    DEBUG=1 $0 ${SCHOLAR_ID} ${OUTPUT_DIR}"
    yellow "  Or inspect the raw HTML:"
    yellow "    head -200 ${DATA_DIR}/scholar_page_0.html"
    die "No publications found."
fi

#=======================  STEP 2: RESOLVE DOIs & PDFs  =========================
blue "╔══════════════════════════════════════════════════════════════╗"
blue "║  Step 2 — Looking for DOIs and open-access PDFs             ║"
blue "╚══════════════════════════════════════════════════════════════╝"

if [[ "$SKIP_DOI" == "1" ]]; then
    yellow "  Skipping DOI/PDF resolution (SKIP_DOI=1)."
    cp "${DATA_DIR}/publications.json" "${DATA_DIR}/publications_enriched.json"
    DOI_COUNT=0
    PDF_COUNT=0
else

cat > "${DATA_DIR}/_resolve_dois.py" << 'PYEOF'
#!/usr/bin/env python3
"""
For each publication, try to find a DOI via CrossRef and an open-access PDF
via Unpaywall. Outputs enriched JSON.
"""

import json
import os
import re
import sys
import signal
import time
import urllib.request
import urllib.parse
import urllib.error

DELAY = 1.5
UNPAYWALL_EMAIL = os.environ.get('UNPAYWALL_EMAIL', '')
DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else '.'
PDF_DIR = sys.argv[2] if len(sys.argv) > 2 else './pdfs'

with open(os.path.join(DATA_DIR, 'publications.json'), 'r') as f:
    pubs = json.load(f)

# On SIGINT, dump whatever we have so far and exit
def handle_interrupt(sig, frame):
    print(f"\n  Saving {len(pubs)} entries (partial DOI data) ...", file=sys.stderr)
    json.dump(pubs, sys.stdout, ensure_ascii=False, indent=2)
    sys.exit(0)

signal.signal(signal.SIGINT, handle_interrupt)

def api_get(url, timeout=15):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'ScholarScript/1.0 (mailto:scholar-script@example.com)'
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8', errors='replace'))
    except Exception:
        return None

def find_doi(title, authors=''):
    query = urllib.parse.quote(title)
    url = f"https://api.crossref.org/works?query.title={query}&rows=3"
    data = api_get(url)
    if not data or 'message' not in data:
        return None
    items = data['message'].get('items', [])
    title_lower = title.lower().strip()
    for item in items:
        item_titles = [t.lower().strip() for t in item.get('title', [])]
        for it in item_titles:
            t_words = set(re.findall(r'\w+', title_lower))
            i_words = set(re.findall(r'\w+', it))
            if not t_words:
                continue
            overlap = len(t_words & i_words) / max(len(t_words), 1)
            if overlap >= 0.75:
                return item.get('DOI')
    return None

def find_pdf_url(doi):
    if not UNPAYWALL_EMAIL or not doi:
        return None
    url = f"https://api.unpaywall.org/v2/{doi}?email={urllib.parse.quote(UNPAYWALL_EMAIL)}"
    data = api_get(url)
    if not data:
        return None
    best = data.get('best_oa_location')
    if best:
        return best.get('url_for_pdf') or best.get('url')
    return None

def download_pdf(url, filepath):
    req = urllib.request.Request(url, headers={'User-Agent': 'ScholarScript/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
            if content[:5] == b'%PDF-':
                with open(filepath, 'wb') as f:
                    f.write(content)
                return True
    except Exception:
        pass
    return False

total = len(pubs)
for idx, pub in enumerate(pubs):
    title = pub.get('title', 'Untitled')
    short_title = title[:70] + ('...' if len(title) > 70 else '')
    print(f"  [{idx+1}/{total}] {short_title}", file=sys.stderr, flush=True)

    doi = find_doi(title, pub.get('authors', ''))
    pub['doi'] = doi or ''
    if doi:
        print(f"       ↳ DOI: {doi}", file=sys.stderr, flush=True)
    time.sleep(DELAY)

    pdf_url = find_pdf_url(doi) if doi else None
    pub['pdf_url'] = pdf_url or ''

    pub['pdf_file'] = ''
    if pdf_url:
        safe_name = re.sub(r'[^a-zA-Z0-9]', '_', title)[:80] + '.pdf'
        filepath = os.path.join(PDF_DIR, safe_name)
        print(f"       ↳ Downloading PDF ...", file=sys.stderr, flush=True)
        if download_pdf(pdf_url, filepath):
            pub['pdf_file'] = filepath
            print(f"       ✓ Saved: {safe_name}", file=sys.stderr, flush=True)
        else:
            print(f"       ✗ Download failed or not a valid PDF", file=sys.stderr, flush=True)
        time.sleep(DELAY)

json.dump(pubs, sys.stdout, ensure_ascii=False, indent=2)
PYEOF

yellow "  Resolving DOIs via CrossRef and looking for PDFs ..."
yellow "  (This takes ~2 sec per publication — ${PUB_COUNT} pubs ≈ $((PUB_COUNT * 2 / 60)) min)"
yellow "  Press Ctrl+C to skip DOI resolution; the PDF will still be generated."
yellow ""

# Trap Ctrl+C during DOI resolution: fall back to base data instead of dying
doi_interrupted=0
trap 'doi_interrupted=1; echo ""; yellow "  ⚠ Interrupted — skipping remaining DOI lookups."' INT

# stdout → JSON file, stderr → terminal (progress messages)
python3 "${DATA_DIR}/_resolve_dois.py" "$DATA_DIR" "$PDF_DIR" \
    > "${DATA_DIR}/publications_enriched.json" || true

trap - INT  # restore default signal handling

if [[ ! -s "${DATA_DIR}/publications_enriched.json" ]]; then
    yellow "  DOI resolution produced no output — using base data."
    cp "${DATA_DIR}/publications.json" "${DATA_DIR}/publications_enriched.json"
fi

DOI_COUNT=$(jq '[.[] | select(.doi != "" and .doi != null)] | length' "${DATA_DIR}/publications_enriched.json" 2>/dev/null || echo 0)
PDF_COUNT=$(jq '[.[] | select(.pdf_file != "" and .pdf_file != null)] | length' "${DATA_DIR}/publications_enriched.json" 2>/dev/null || echo 0)
green "  DOIs found: ${DOI_COUNT}/${PUB_COUNT}"
green "  PDFs downloaded: ${PDF_COUNT}/${PUB_COUNT}"

fi  # end SKIP_DOI check

#=======================  STEP 2b: COLLECT BibTeX  =============================
blue "╔══════════════════════════════════════════════════════════════╗"
blue "║  Step 2b — Collecting BibTeX entries                        ║"
blue "╚══════════════════════════════════════════════════════════════╝"

BIBTEX_DIR="${OUTPUT_DIR}/bibtex"
mkdir -p "$BIBTEX_DIR"

cat > "${DATA_DIR}/_collect_bibtex.py" << 'PYEOF'
#!/usr/bin/env python3
"""
Collect BibTeX entries for all publications.
- For publications with a DOI: fetch BibTeX from CrossRef (content negotiation).
- For publications without a DOI: generate a synthetic @misc BibTeX entry.
Outputs:
  - One .bib file per publication in bibtex/
  - One combined publications.bib with all entries
"""

import json
import os
import re
import signal
import sys
import time
import unicodedata
import urllib.request
import urllib.error

DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else '.'
BIBTEX_DIR = sys.argv[2] if len(sys.argv) > 2 else './bibtex'
DELAY = 1.0

with open(os.path.join(DATA_DIR, 'publications_enriched.json'), 'r') as f:
    pubs = json.load(f)

all_bibtex = []
stats = {'crossref': 0, 'generated': 0, 'failed': 0}

def handle_interrupt(sig, frame):
    """On Ctrl+C, write whatever we have so far."""
    print(f"\n  Saving {len(all_bibtex)} BibTeX entries (partial) ...", file=sys.stderr)
    write_combined()
    sys.exit(0)

signal.signal(signal.SIGINT, handle_interrupt)

def make_cite_key(pub):
    """Generate a citation key: FirstAuthorLastName_Year_FirstTitleWord."""
    authors = pub.get('authors', '')
    year = pub.get('year', 'XXXX') or 'XXXX'
    title = pub.get('title', 'untitled')

    # Extract first author's last name
    first_author = authors.split(',')[0].strip() if authors else 'Unknown'
    # Last name is typically the last word (handles "First Last" and "Last, First")
    parts = first_author.split()
    last_name = parts[-1] if parts else 'Unknown'

    # First meaningful title word (skip short words)
    skip = {'a','an','the','on','in','of','for','and','with','to','from','by'}
    title_words = re.findall(r'[A-Za-z]+', title)
    title_word = 'untitled'
    for w in title_words:
        if w.lower() not in skip and len(w) > 2:
            title_word = w
            break

    # Clean to ASCII
    key = f"{last_name}_{year}_{title_word}"
    key = unicodedata.normalize('NFKD', key).encode('ascii', 'ignore').decode()
    key = re.sub(r'[^a-zA-Z0-9_]', '', key)
    return key

def fetch_bibtex_from_doi(doi):
    """Fetch BibTeX from doi.org using content negotiation."""
    url = f"https://doi.org/{doi}"
    req = urllib.request.Request(url, headers={
        'Accept': 'application/x-bibtex',
        'User-Agent': 'ScholarScript/1.0'
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            bib = resp.read().decode('utf-8', errors='replace')
            # Sanity: does it look like BibTeX?
            if '@' in bib and '{' in bib:
                return bib.strip()
    except Exception as e:
        pass
    return None

def escape_bibtex(text):
    """Escape special chars for BibTeX values."""
    if not text:
        return ''
    # Protect existing braces, then wrap special chars
    text = text.replace('&', r'\&')
    text = text.replace('%', r'\%')
    text = text.replace('$', r'\$')
    text = text.replace('#', r'\#')
    text = text.replace('_', r'\_')
    return text

def generate_bibtex(pub, cite_key):
    """Generate a synthetic BibTeX entry from Scholar metadata."""
    title = pub.get('title', 'Untitled')
    authors = pub.get('authors', 'Unknown')
    year = pub.get('year', '') or ''
    venue = pub.get('venue', '')
    doi = pub.get('doi', '')
    scholar_link = pub.get('scholar_link', '')

    # Convert author format: "A Name, B Name, C Name" → "Name, A and Name, B and Name, C"
    # Scholar format is usually "First Last, First Last, ..."
    author_list = [a.strip() for a in authors.split(',') if a.strip()]
    bib_authors = ' and '.join(author_list)

    # Guess entry type from venue
    entry_type = '@article'
    if venue:
        v_lower = venue.lower()
        if any(kw in v_lower for kw in ['conference', 'proceedings', 'workshop', 'symposium', 'icml', 'neurips', 'iclr', 'cvpr', 'iccv', 'eccv', 'aaai', 'ijcai', 'acl', 'emnlp', 'naacl', 'aiaa', 'asme']):
            entry_type = '@inproceedings'
        elif any(kw in v_lower for kw in ['book', 'springer', 'lecture notes', 'chapter']):
            entry_type = '@incollection'
        elif any(kw in v_lower for kw in ['thesis', 'dissertation']):
            entry_type = '@phdthesis'
        elif any(kw in v_lower for kw in ['arxiv', 'preprint']):
            entry_type = '@misc'

    lines = [f"{entry_type}{{{cite_key},"]
    lines.append(f"  title = {{{escape_bibtex(title)}}},")
    lines.append(f"  author = {{{escape_bibtex(bib_authors)}}},")
    if year:
        lines.append(f"  year = {{{year}}},")
    if venue:
        if entry_type == '@inproceedings':
            lines.append(f"  booktitle = {{{escape_bibtex(venue)}}},")
        elif entry_type == '@article':
            lines.append(f"  journal = {{{escape_bibtex(venue)}}},")
        else:
            lines.append(f"  note = {{{escape_bibtex(venue)}}},")
    if doi:
        lines.append(f"  doi = {{{doi}}},")
    if scholar_link:
        lines.append(f"  url = {{{scholar_link}}},")
    lines.append("}")

    return '\n'.join(lines)

def write_combined():
    """Write the combined .bib file."""
    combined_path = os.path.join(BIBTEX_DIR, 'publications.bib')
    with open(combined_path, 'w', encoding='utf-8') as f:
        f.write("% Combined BibTeX file — auto-generated by scholar_publications.sh\n")
        f.write(f"% {len(all_bibtex)} entries\n")
        f.write(f"% Generated from Google Scholar profile\n\n")
        f.write('\n\n'.join(all_bibtex))
        f.write('\n')

# Process each publication
total = len(pubs)
used_keys = set()

for idx, pub in enumerate(pubs):
    title = pub.get('title', 'Untitled')
    doi = pub.get('doi', '')
    short_title = title[:65] + ('...' if len(title) > 65 else '')
    print(f"  [{idx+1}/{total}] {short_title}", file=sys.stderr, flush=True)

    cite_key = make_cite_key(pub)
    # Ensure unique keys
    base_key = cite_key
    counter = 2
    while cite_key in used_keys:
        cite_key = f"{base_key}_{counter}"
        counter += 1
    used_keys.add(cite_key)

    bib_entry = None

    # Try CrossRef first if we have a DOI
    if doi:
        bib_entry = fetch_bibtex_from_doi(doi)
        if bib_entry:
            # Replace the CrossRef cite key with our consistent one
            bib_entry = re.sub(r'(@\w+)\{[^,]+,', rf'\1{{{cite_key},', bib_entry, count=1)
            stats['crossref'] += 1
            print(f"       ✓ BibTeX from CrossRef (DOI)", file=sys.stderr, flush=True)
        time.sleep(DELAY)

    # Fall back to generating from metadata
    if not bib_entry:
        bib_entry = generate_bibtex(pub, cite_key)
        stats['generated'] += 1
        src = "Scholar metadata"
        print(f"       ↳ BibTeX generated from {src}", file=sys.stderr, flush=True)

    # Save individual .bib file and .txt copy (for clickable PDF links)
    safe_base = re.sub(r'[^a-zA-Z0-9_]', '_', cite_key)[:80]
    bib_name = safe_base + '.bib'
    txt_name = safe_base + '.txt'
    for fname in (bib_name, txt_name):
        fpath = os.path.join(BIBTEX_DIR, fname)
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(bib_entry + '\n')

    # Store filenames on the pub for LaTeX references
    pub['bib_file'] = bib_name
    pub['bib_txt_file'] = txt_name
    pub['cite_key'] = cite_key

    all_bibtex.append(bib_entry)

# Write combined file
write_combined()

# Re-save enriched JSON with bib_file fields added
enriched_path = os.path.join(os.path.dirname(BIBTEX_DIR), 'data', 'publications_enriched.json')
with open(enriched_path, 'w', encoding='utf-8') as f:
    json.dump(pubs, f, ensure_ascii=False, indent=2)

print(f"\n  Summary: {stats['crossref']} from CrossRef, "
      f"{stats['generated']} generated from metadata, "
      f"{stats['failed']} failed", file=sys.stderr)
PYEOF

yellow "  Fetching BibTeX entries (${PUB_COUNT} publications) ..."
yellow "  (CrossRef lookup for DOIs + generated entries for the rest)"
yellow ""

trap 'echo ""; yellow "  ⚠ Interrupted — partial BibTeX saved."' INT
python3 "${DATA_DIR}/_collect_bibtex.py" "$DATA_DIR" "$BIBTEX_DIR" || true
trap - INT

BIB_COUNT=$(ls -1 "$BIBTEX_DIR"/*.bib 2>/dev/null | grep -v publications.bib | wc -l || echo 0)
green "  ✓ ${BIB_COUNT} individual .bib files saved to ${BIBTEX_DIR}/"
if [[ -f "${BIBTEX_DIR}/publications.bib" ]]; then
    green "  ✓ Combined file: ${BIBTEX_DIR}/publications.bib"
fi

#===========================  STEP 3: GENERATE LaTeX  ==========================
blue "╔══════════════════════════════════════════════════════════════╗"
blue "║  Step 3 — Generating LaTeX publication list                 ║"
blue "╚══════════════════════════════════════════════════════════════╝"

cat > "${DATA_DIR}/_generate_tex.py" << 'PYEOF'
#!/usr/bin/env python3
"""Generate a LaTeX document from the enriched publications JSON."""

import json
import os
import re
import sys
from collections import Counter

DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else '.'
TEX_FILE = sys.argv[2] if len(sys.argv) > 2 else 'publications.tex'
SCHOLAR_ID = sys.argv[3] if len(sys.argv) > 3 else ''

with open(os.path.join(DATA_DIR, 'publications_enriched.json'), 'r') as f:
    pubs = json.load(f)

def tex_escape(text):
    if not text:
        return ''
    for old, new in [('&',r'\&'),('%',r'\%'),('$',r'\$'),('#',r'\#'),
                     ('_',r'\_'),('{',r'\{'),('}',r'\}'),
                     ('~',r'\textasciitilde{}'),('^',r'\textasciicircum{}')]:
        text = text.replace(old, new)
    return text

by_year = {}
for pub in pubs:
    year = pub.get('year', '') or 'Undated'
    by_year.setdefault(year, []).append(pub)

sorted_years = sorted(by_year.keys(), key=lambda y: (y if y != 'Undated' else '0000'), reverse=True)

# Auto-detect author name for bolding
author_name = ''
if pubs:
    first_authors = []
    for p in pubs[:15]:
        authors = p.get('authors', '')
        if authors:
            first = authors.split(',')[0].strip()
            if first:
                first_authors.append(first)
    if first_authors:
        author_name = Counter(first_authors).most_common(1)[0][0]

total_citations = sum(p.get('citations', 0) for p in pubs)

lines = []
lines.append(r"""\documentclass[11pt,a4paper]{article}
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
    pdfauthor={Generated by scholar\_publications.sh},
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

if SCHOLAR_ID:
    lines.append(r"  {\small\href{https://scholar.google.com/citations?user=" +
                 SCHOLAR_ID + r"}{Google Scholar Profile}} \\[0.3em]")

lines.append(r"  {\small\textcolor{gray}{Auto-generated \today{} ~$\cdot$~ " +
             tex_escape(str(len(pubs))) + r" publications ~$\cdot$~ " +
             tex_escape(str(total_citations)) + r" total citations}}")
lines.append(r"""\end{center}
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
    lines.append(r'\section*{' + tex_escape(year) + '}')
    year_pubs = sorted(by_year[year], key=lambda p: p.get('citations', 0), reverse=True)
    start_num = pub_number - len(year_pubs) + 1
    lines.append(r'\begin{enumerate}[label={\textbf{[\arabic*]}},start=' +
                 str(start_num) + r',leftmargin=2.5em]')

    for pub in year_pubs:
        title = tex_escape(pub.get('title', 'Untitled'))
        authors = tex_escape(pub.get('authors', ''))
        venue = tex_escape(pub.get('venue', ''))
        doi = pub.get('doi', '')
        pdf_file = pub.get('pdf_file', '')
        bib_txt_file = pub.get('bib_txt_file', '')
        scholar_link = pub.get('scholar_link', '')
        citations = pub.get('citations', 0)

        if author_name:
            esc_name = tex_escape(author_name)
            authors = authors.replace(esc_name, r'\textbf{' + esc_name + '}')

        item_lines = [r'  \item \textbf{' + title + r'}']
        if authors:
            item_lines.append(r'  \\ ' + authors)
        if venue:
            item_lines.append(r'  \\ \textit{' + venue + r'}')

        links = []
        if doi:
            links.append(r'\href{https://doi.org/' + doi + r'}{\textcolor{NavyBlue}{\small[\,DOI\,]}}')
        if pdf_file:
            rel_path = os.path.basename(pdf_file)
            links.append(r'\href{./pdfs/' + rel_path + r'}{\textcolor{ForestGreen}{\small[\,PDF\,]}}')
        if bib_txt_file:
            links.append(r'\href{./bibtex/' + bib_txt_file + r'}{\textcolor{Bittersweet}{\small[\,BIB\,]}}')
        if scholar_link:
            esc_link = scholar_link.replace('%',r'\%').replace('#',r'\#').replace('&',r'\&')
            links.append(r'\href{' + esc_link + r'}{\textcolor{gray}{\small[\,Scholar\,]}}')
        if citations > 0:
            links.append(r'{\small\textcolor{gray}{' + str(citations) + r' citations}}')

        if links:
            item_lines.append(r'  \\ ' + ' ~~ '.join(links))

        lines.append('\n'.join(item_lines))
        lines.append('')

    lines.append(r'\end{enumerate}')
    lines.append('')
    pub_number -= len(year_pubs)

lines.append(r'\end{document}')

with open(TEX_FILE, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))
print(f"  LaTeX written to {TEX_FILE}", file=sys.stderr)
PYEOF

python3 "${DATA_DIR}/_generate_tex.py" "$DATA_DIR" "$TEX_FILE" "$SCHOLAR_ID"
green "  TeX file created: ${TEX_FILE}"

#===========================  STEP 4: COMPILE PDF  =============================
blue "╔══════════════════════════════════════════════════════════════╗"
blue "║  Step 4 — Compiling LaTeX → PDF                             ║"
blue "╚══════════════════════════════════════════════════════════════╝"

cd "$OUTPUT_DIR"
yellow "  Running pdflatex (pass 1/2) ..."
pdflatex -interaction=nonstopmode -halt-on-error "publications.tex" > /dev/null 2>&1 || true
yellow "  Running pdflatex (pass 2/2) ..."
pdflatex -interaction=nonstopmode -halt-on-error "publications.tex" > /dev/null 2>&1 || true

if [[ -f "publications.pdf" ]]; then
    green "  ✓ PDF compiled successfully: ${PDF_OUTPUT}"
else
    red "  ✗ PDF compilation failed. Check ${OUTPUT_DIR}/publications.log for errors."
    yellow "    You can also compile manually: cd ${OUTPUT_DIR} && pdflatex publications.tex"
fi
cd - > /dev/null

#================================  SUMMARY  ====================================
echo ""
blue "╔══════════════════════════════════════════════════════════════╗"
blue "║  Done!                                                      ║"
blue "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Output directory:  ${OUTPUT_DIR}/"
echo "  ├── publications.pdf       ← Your clickable publication list"
echo "  ├── publications.tex       ← LaTeX source (customise as needed)"
echo "  ├── bibtex/"
echo "  │   ├── publications.bib   ← All entries in one file"
echo "  │   └── *.bib              ← One file per publication"
echo "  ├── pdfs/                  ← Downloaded open-access PDFs"
echo "  └── data/"
echo "      ├── publications.json  ← Raw parsed data"
echo "      └── publications_enriched.json  ← With DOIs & PDF links"
echo ""
if (( PDF_COUNT > 0 )); then
    green "  ${PDF_COUNT} PDF(s) were downloaded to ${PDF_DIR}/"
fi
if [[ -n "$UNPAYWALL_EMAIL" ]]; then
    echo "  Unpaywall was used for PDF lookups (email: ${UNPAYWALL_EMAIL})"
else
    yellow "  Tip: Set UNPAYWALL_EMAIL to enable Unpaywall PDF lookups:"
    yellow "    export UNPAYWALL_EMAIL=\"your@email.com\""
fi
echo ""
