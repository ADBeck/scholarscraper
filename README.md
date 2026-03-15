# ScholarScraper v0.1 / ADB

Scrape your Google Scholar profile, resolve DOIs, download open-access PDFs, collect BibTeX entries, and generate a clickable LaTeX publication list — all automated.

Three flavours, same functionality:

| File | Platform | Dependencies |
|------|----------|-------------|
| `scholarscraper_gui.py` | Any (web browser) | Python 3.7+ only |
| `scholarscraper.py` | Linux / macOS / Windows | Python 3.7+ only |
| `scholar_publications.sh` | Linux / macOS | bash, curl, python3, pdflatex, jq |

## Quick Start

### Web GUI (recommended)
```bash
python scholarscraper_gui.py
# Opens http://localhost:8457 in your browser
```

### Command-line (Python)
```bash
python scholarscraper.py r1tm9b4AAAAJ
python scholarscraper.py r1tm9b4AAAAJ --include-pdf ~/Papers --unpaywall-email me@uni.edu
python scholarscraper.py r1tm9b4AAAAJ --skip-doi   # fast mode, no DOI resolution
```

### Command-line (Bash)
```bash
./scholar_publications.sh r1tm9b4AAAAJ
SKIP_DOI=1 ./scholar_publications.sh r1tm9b4AAAAJ
```

## Features

- **Scrapes Google Scholar** profiles (handles pagination, CAPTCHAs, consent pages)
- **Resolves DOIs** via CrossRef API with fuzzy title matching
- **Downloads open-access PDFs** via Unpaywall API
- **Matches local PDFs** from a folder by fuzzy filename comparison (`--include-pdf`)
- **Collects BibTeX** entries from CrossRef (or generates them from metadata)
- **Generates a LaTeX publication list** with clickable links: [DOI], [PDF], [BIB], [Scholar]
- **Compiles to PDF** via pdflatex (or use Overleaf with the `.tex` file)

### GUI-specific features
- Live progress bar and streaming log via Server-Sent Events
- Publication table with search, sort, and filter
- Inline syntax-highlighted BibTeX preview
- One-click copy: individual entry, citation key, or all BibTeX
- Dark/light theme toggle

## Output Structure

```
scholar_output/
├── publications.pdf          ← Clickable publication list
├── publications.tex          ← LaTeX source (customise or upload to Overleaf)
├── bibtex/
│   ├── publications.bib      ← All entries combined
│   ├── Author_2024_Title.bib ← Individual .bib files
│   └── Author_2024_Title.txt ← Same content, opens in text editor from PDF
├── pdfs/                     ← Downloaded open-access PDFs
└── data/
    ├── publications.json
    └── publications_enriched.json
```

## Optional Dependencies

- `pip install beautifulsoup4` — more reliable HTML parsing (recommended)
- `pdflatex` (TeX Live on Linux/Mac, MiKTeX on Windows) — for compiling the PDF

## Tips

- **Google consent page?** Export cookies from your browser and pass with `--cookie` or the Cookie field in the GUI
- **CAPTCHA?** Wait 10–30 minutes and retry, or use a different network
- **No PDFs showing?** You need `--unpaywall-email` or `--include-pdf` — PDFs aren't downloaded by default
- **Ctrl+C** during DOI resolution saves partial progress and continues to LaTeX generation

## License

MIT
