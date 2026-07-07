# ÖSYM PDF parser

Parses KPSS exam PDFs into the Supabase `questions` table. Regex/pdfplumber is
the primary path; a local Lemonade LLM (`--llm`) can retry the residual
failures per `PHASE_C_ADDENDUM_local_llm.md`.

## Setup (Windows, Python 3.11+)

```powershell
cd kipss\parser
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install pdfplumber requests
# only if you will use --llm (Lemonade must be running on localhost:13305):
pip install openai
```

## Workflow

```powershell
# 1. parse (writes out\<source>.json + out\<source>_needs_review.json)
python parse_osym.py "C:\path\to\exam.pdf" --source KPSS-2022-GK `
    --tarih 1-27 --cografya 28-45 --vatandaslik 46-54 --guncel 55-60

# 2. REVIEW: check 10 random questions + answer keys against the PDF

# 3. insert the reviewed clean rows
python parse_osym.py "C:\path\to\exam.pdf" --source KPSS-2022-GK `
    --tarih 1-27 --cografya 28-45 --vatandaslik 46-54 --guncel 55-60 --insert
```

Optional LLM pass on gate failures (adds `out\<source>_llm_assisted.json`;
those rows are NEVER inserted by `--insert` — review EVERY row against the
PDF, then run with `--insert-llm-assisted`):

```powershell
python parse_osym.py ... --llm
python parse_osym.py ... --insert-llm-assisted
```

## Multi-test books (the two soru bankası PDFs)

These books contain many tests; question numbering restarts at 1 in each.
Scope one test per run with `--pages` and give each its own `--source`:

```powershell
python parse_osym.py "book.pdf" --pages 12-20 --source AKSOY-2022-D01 --tarih 1-30
```

If extraction looks broken on some layout, dump one failing page and send it:

```powershell
python parse_osym.py "book.pdf" --dump-page 13 --source X --tarih 1-30
```
