#!/usr/bin/env python3
"""ÖSYM PDF -> Supabase question parser (KPSS quiz app, Phase C).

Primary path is pdfplumber + regex. Questions that fail the quality gate can
optionally be retried through a local Lemonade LLM (--llm) per
PHASE_C_ADDENDUM_local_llm.md; LLM-touched rows never merge into the main
output and need their own review + --insert-llm-assisted run.

Typical flow:
  python parse_osym.py exam.pdf --source KPSS-2022-GK \
      --tarih 1-27 --cografya 28-45 --vatandaslik 46-54 --guncel 55-60
  # review out/KPSS-2022-GK.json + out/KPSS-2022-GK_needs_review.json, then:
  python parse_osym.py exam.pdf --source KPSS-2022-GK ... --insert

Multi-test books: use --pages to scope one deneme per run (numbering restarts
at 1 in each test). If a layout breaks, run --dump-page N and send the output.
"""

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

SUPABASE_URL = "https://rknepvongsjlqodnkbtg.supabase.co"
# anon key only — never a service key (RLS intentionally off, two trusted users)
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJrbmVwdm9uZ3NqbHFvZG5rYnRnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODMzNTAxNzYsImV4cCI6MjA5ODkyNjE3Nn0."
    "5smHl86AN9x6gZIWW0416UP1xUawEWqjBIRmadRokgE"
)

LEMONADE_BASE_URL = "http://localhost:13305/api/v1"
LEMONADE_MODEL = "gpt-oss-20b-mxfp4-GGUF"
# ctx_size on the server is 4096; keep prompt+input well under ~2500 tokens.
# Turkish runs ~3 chars/token, so cap the raw block around 7000 chars.
LLM_MAX_RAW_CHARS = 7000
LLM_SYSTEM_PROMPT = (
    "You are extracting structured data from the exact text provided. If the "
    "question text, an option, or the correct answer is not clearly present in "
    "the given text, respond with null for that field. Never infer, guess, or "
    "complete missing content from general knowledge. It is always better to "
    "return null than to fabricate a plausible-looking value."
)

MAX_QUESTION_NO = 60
KEY_MIN_PAIRS = 8          # a page with at least this many "N. X" pairs is an answer-key page
IMAGE_STEM_CHARS = 15      # stem shorter than this => probably an image/map/table question

QNUM_RE = re.compile(r"^[ \t]*(\d{1,2})\.[ \t]*")
OPT_LINE_RE = re.compile(r"(?m)^[ \t]*([A-E])\)[ \t]*")
KEYPAIR_RE = re.compile(r"\b(\d{1,2})\s*[.)]\s*([A-E])\b")


def nfc(s):
    return unicodedata.normalize("NFC", s)


def parse_range(spec, name):
    m = re.fullmatch(r"(\d+)-(\d+)", spec)
    if not m or int(m.group(1)) > int(m.group(2)):
        sys.exit(f"bad --{name} range: {spec!r} (expected e.g. 1-27)")
    return int(m.group(1)), int(m.group(2))


def category_for(no, ranges):
    for cat, (lo, hi) in ranges.items():
        if lo <= no <= hi:
            return cat
    return None


# --------------------------------------------------------------------------
# PDF extraction: split columns by x BEFORE top-to-bottom reading, and keep
# text lines + image positions in one reading-order event stream.
# --------------------------------------------------------------------------

def page_column_events(page):
    """Yield per-column lists of events ({'kind':'line','text':...} or {'kind':'image'})
    in reading order: left column fully, then right column."""
    mid = page.width / 2
    columns = []
    for x0, x1 in ((0, mid), (mid, page.width)):
        crop = page.crop((x0, 0, x1, page.height))
        events = []
        for ln in crop.extract_text_lines():
            text = nfc(ln["text"]).strip()
            if text:
                events.append({"kind": "line", "text": text, "top": ln["top"]})
        for img in page.images:
            cx = (img["x0"] + img["x1"]) / 2
            if x0 <= cx < x1:
                events.append({"kind": "image", "top": img["top"]})
        events.sort(key=lambda e: e["top"])
        columns.append(events)
    return columns


def find_answer_key(pages_text):
    """Detect answer-key pages (dense 'N. X' pairs) and build {no: letter}.
    Returns (key_map, key_page_indexes, conflicts)."""
    key, key_pages, conflicts = {}, set(), []
    for idx, text in pages_text:
        pairs = KEYPAIR_RE.findall(text)
        if len(pairs) < KEY_MIN_PAIRS:
            continue
        key_pages.add(idx)
        for no_s, letter in pairs:
            no = int(no_s)
            if no < 1 or no > MAX_QUESTION_NO:
                continue
            if no in key and key[no] != letter:
                conflicts.append((no, key[no], letter))
            else:
                key.setdefault(no, letter)
    return key, key_pages, conflicts


def extract_questions(pdf, page_filter, start_no=1):
    """Walk the event stream and segment into raw question blocks.

    Question markers must be sequential (expected or expected+1, logging the
    gap) so years like '1923.' or stray numbers can't open a bogus question.
    """
    pages_text = []
    for i, page in enumerate(pdf.pages, start=1):
        if page_filter and i not in page_filter:
            continue
        pages_text.append((i, nfc(page.extract_text() or "")))

    key, key_pages, key_conflicts = find_answer_key(pages_text)

    questions, missing = [], []
    expected = start_no
    current = None
    for i, page in enumerate(pdf.pages, start=1):
        if (page_filter and i not in page_filter) or i in key_pages:
            continue
        for events in page_column_events(page):
            for ev in events:
                if ev["kind"] == "image":
                    if current:
                        current["saw_image"] = True
                    continue
                m = QNUM_RE.match(ev["text"])
                no = int(m.group(1)) if m else None
                if no is not None and no in (expected, expected + 1) and no <= MAX_QUESTION_NO:
                    if current:
                        questions.append(current)
                    if no == expected + 1:
                        missing.append(expected)
                    current = {"no": no, "lines": [ev["text"][m.end():]], "saw_image": False}
                    expected = no + 1
                elif current:
                    current["lines"].append(ev["text"])
    if current:
        questions.append(current)
    for q in questions:
        q["raw"] = "\n".join(q["lines"]).strip()
        del q["lines"]
    return questions, key, missing, key_conflicts


# --------------------------------------------------------------------------
# Per-question parsing + quality gate
# --------------------------------------------------------------------------

def find_option_marks(raw):
    """Return [(letter, start, end), ...] for A)-E) in order, or None."""
    line_marks = list(OPT_LINE_RE.finditer(raw))
    if [m.group(1) for m in line_marks] == list("ABCDE"):
        return [(m.group(1), m.start(), m.end()) for m in line_marks]
    # fallback: inline chain scan (options may not start their own lines)
    marks, pos = [], 0
    for letter in "ABCDE":
        m = re.compile(
            rf"(?<![A-Za-zÇĞİÖŞÜçğıöşü0-9]){letter}\)\s*"
        ).search(raw, pos)
        if not m:
            return None
        marks.append((letter, m.start(), m.end()))
        pos = m.end()
    return marks


def parse_question(q):
    """Turn a raw block into a row dict; returns (row, reasons)."""
    reasons = []
    marks = find_option_marks(q["raw"])
    if not marks:
        return None, ["could not find exactly 5 options A)-E) in order"]
    stem = q["raw"][: marks[0][1]].strip()
    options = {}
    for i, (letter, _, end) in enumerate(marks):
        nxt = marks[i + 1][1] if i + 1 < len(marks) else len(q["raw"])
        options[letter] = re.sub(r"\s+", " ", q["raw"][end:nxt]).strip()
    if any(not v for v in options.values()):
        reasons.append("one or more empty option texts")
    if not stem:
        reasons.append("empty question_text")
    stem = "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in stem.splitlines())
    has_image = q["saw_image"] or len(stem) < IMAGE_STEM_CHARS
    row = {
        "question_no": q["no"],
        "question_text": nfc(stem),
        "options": {k: nfc(v) for k, v in options.items()},
        "has_image": has_image,
    }
    return row, reasons


def gate(row, key, ranges):
    reasons = []
    cat = category_for(row["question_no"], ranges)
    if cat is None:
        reasons.append(f"question_no {row['question_no']} outside all category ranges")
    ans = key.get(row["question_no"])
    if ans is None:
        reasons.append(f"no answer key entry for question {row['question_no']}")
    if len(row["options"]) != 5 or set(row["options"]) != set("ABCDE"):
        reasons.append("options are not exactly A-E")
    if not row["question_text"]:
        reasons.append("empty question_text")
    return cat, ans, reasons


# --------------------------------------------------------------------------
# LLM fallback (Lemonade, per addendum) — opt-in via --llm
# --------------------------------------------------------------------------

def llm_extract(client, raw):
    """One guarded extraction call. Returns (data|None, fail_reason|None)."""
    if len(raw) > LLM_MAX_RAW_CHARS:
        return None, f"raw block too large for ctx window ({len(raw)} chars)"
    schema = {
        "type": "object",
        "properties": {
            "question_no": {"type": ["integer", "null"]},
            "question_text": {"type": ["string", "null"]},
            "options": {
                "type": "object",
                "properties": {k: {"type": ["string", "null"]} for k in "ABCDE"},
                "required": list("ABCDE"),
                "additionalProperties": False,
            },
        },
        "required": ["question_no", "question_text", "options"],
        "additionalProperties": False,
    }
    resp = client.chat.completions.create(
        model=LEMONADE_MODEL,
        temperature=0,
        max_tokens=1024,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "osym_question", "strict": True, "schema": schema},
        },
        messages=[
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Extract the single multiple-choice exam question from the raw "
                    "text below. Return JSON with question_no (the printed question "
                    "number), question_text (without the number or the options), and "
                    "options A-E.\n\n<raw>\n" + raw + "\n</raw>"
                ),
            },
        ],
    )
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        return None, "truncated response (finish_reason=length)"
    try:
        data = json.loads(choice.message.content)
    except (json.JSONDecodeError, TypeError):
        return None, "unparseable JSON from LLM"
    return data, None


def llm_pass(candidates, key, ranges, source):
    """Retry gate failures through Lemonade. Returns (assisted_rows, still_failed)."""
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("--llm requires the openai package: pip install openai")
    client = OpenAI(base_url=LEMONADE_BASE_URL, api_key="lemonade")
    assisted, still_failed = [], []
    for q, orig_reasons in candidates:
        entry = {"question_no": q["no"], "regex_reasons": orig_reasons, "raw_text": q["raw"]}
        try:
            data, fail = llm_extract(client, q["raw"])
        except Exception as e:  # server down, connection refused, etc.
            data, fail = None, f"LLM call failed: {e}"
        if fail is None:
            nulls = [f for f in ("question_no", "question_text") if data.get(f) is None]
            opts = data.get("options") or {}
            nulls += [f"options.{k}" for k in "ABCDE" if opts.get(k) is None]
            if nulls:
                fail = "LLM returned null for: " + ", ".join(nulls)
        if fail is None:
            no = data["question_no"]
            cat = category_for(no, ranges)
            ans = key.get(no)
            if cat is None:
                fail = f"question_no {no} outside all category ranges"
            elif ans is None:
                fail = f"no answer key entry for question {no}"
        if fail:
            entry["status"] = f"failed: {fail}"
            still_failed.append({"question_no": q["no"], "reasons": orig_reasons + [fail],
                                 "raw_text": q["raw"]})
        else:
            entry["status"] = "ok"
            entry["row"] = {
                "category": cat,
                "source": source,
                "question_no": no,
                "question_text": nfc(data["question_text"]),
                "options": {k: nfc(opts[k]) for k in "ABCDE"},
                "correct_answer": ans,
                "has_image": q["saw_image"],
            }
        assisted.append(entry)
    return assisted, still_failed


# --------------------------------------------------------------------------
# Supabase insert (REST, anon key)
# --------------------------------------------------------------------------

def insert_rows(rows, label):
    import requests

    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json; charset=utf-8",
        "Prefer": "return=minimal",
    }
    total = 0
    for i in range(0, len(rows), 50):
        batch = rows[i : i + 50]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/questions",
            headers=headers,
            data=json.dumps(batch, ensure_ascii=False).encode("utf-8"),
            timeout=30,
        )
        if r.status_code != 201:
            sys.exit(f"insert failed at batch {i//50} ({label}): HTTP {r.status_code}: {r.text}")
        total += len(batch)
    # verify: count what the API now holds for this source
    src = rows[0]["source"]
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/questions?select=q_id&source=eq.{src}",
        headers={**headers, "Prefer": "count=exact", "Range": "0-0"},
        timeout=30,
    )
    count = r.headers.get("Content-Range", "?/?").split("/")[-1]
    print(f"inserted {total} rows ({label}); API now reports {count} rows with source={src}")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", help="path to the exam PDF")
    ap.add_argument("--source", required=True, help="source tag, e.g. KPSS-2022-GK")
    ap.add_argument("--tarih", help="question range, e.g. 1-27")
    ap.add_argument("--cografya", help="question range, e.g. 28-45")
    ap.add_argument("--vatandaslik", help="question range, e.g. 46-54")
    ap.add_argument("--guncel", help="question range, e.g. 55-60")
    ap.add_argument("--pages", help="only parse this PDF page range, e.g. 5-12 (for multi-test books)")
    ap.add_argument("--start-no", type=int, default=1, help="first expected question number (default 1)")
    ap.add_argument("--dump-page", type=int, help="print raw per-column text of PDF page N and exit")
    ap.add_argument("--llm", action="store_true", help="retry gate failures via local Lemonade LLM")
    ap.add_argument("--insert", action="store_true", help="insert reviewed out/<source>.json rows")
    ap.add_argument("--insert-llm-assisted", action="store_true",
                    help="insert reviewed ok-rows from out/<source>_llm_assisted.json")
    args = ap.parse_args()

    out_dir = Path("out")
    main_path = out_dir / f"{args.source}.json"
    review_path = out_dir / f"{args.source}_needs_review.json"
    llm_path = out_dir / f"{args.source}_llm_assisted.json"

    if args.insert or args.insert_llm_assisted:
        if args.insert:
            rows = json.loads(main_path.read_text(encoding="utf-8"))
            if not rows:
                sys.exit(f"{main_path} is empty, nothing to insert")
            insert_rows(rows, "main")
        if args.insert_llm_assisted:
            entries = json.loads(llm_path.read_text(encoding="utf-8"))
            rows = [e["row"] for e in entries if e.get("status") == "ok"]
            if not rows:
                sys.exit(f"{llm_path} has no ok rows to insert")
            insert_rows(rows, "llm-assisted")
        return

    import pdfplumber

    ranges = {}
    for cat in ("tarih", "cografya", "vatandaslik", "guncel"):
        spec = getattr(args, cat)
        if spec:
            ranges[cat] = parse_range(spec, cat)
    if not ranges:
        sys.exit("at least one category range is required (e.g. --tarih 1-27)")

    page_filter = None
    if args.pages:
        lo, hi = parse_range(args.pages, "pages")
        page_filter = set(range(lo, hi + 1))

    with pdfplumber.open(args.pdf) as pdf:
        if args.dump_page:
            page = pdf.pages[args.dump_page - 1]
            mid = page.width / 2
            for name, box in (("LEFT", (0, 0, mid, page.height)),
                              ("RIGHT", (mid, 0, page.width, page.height))):
                print(f"===== page {args.dump_page} {name} column =====")
                print(nfc(page.crop(box).extract_text() or "(empty)"))
            return
        questions, key, missing, key_conflicts = extract_questions(pdf, page_filter, args.start_no)

    good, needs_review, llm_candidates = [], [], []
    for q in questions:
        row, reasons = parse_question(q)
        if row is not None:
            cat, ans, gate_reasons = gate(row, key, ranges)
            reasons += gate_reasons
        if row is None or reasons:
            needs_review.append({"question_no": q["no"], "reasons": reasons, "raw_text": q["raw"]})
            llm_candidates.append((q, reasons))
        else:
            good.append({
                "category": cat, "source": args.source, "question_no": row["question_no"],
                "question_text": row["question_text"], "options": row["options"],
                "correct_answer": ans, "has_image": row["has_image"],
            })
    for no in missing:
        entry = {"question_no": no, "reasons": ["question number never matched in reading order"],
                 "raw_text": ""}
        needs_review.append(entry)

    assisted = []
    if args.llm and llm_candidates:
        assisted, still_failed = llm_pass(llm_candidates, key, ranges, args.source)
        # LLM-rescued questions leave needs_review; failures stay (with the LLM reason added)
        rescued = {e["question_no"] for e in assisted if e["status"] == "ok"}
        needs_review = [n for n in needs_review if n["question_no"] not in rescued or not n["raw_text"]]
        for f in still_failed:
            for n in needs_review:
                if n["question_no"] == f["question_no"] and n["raw_text"]:
                    n["reasons"] = f["reasons"]

    out_dir.mkdir(exist_ok=True)
    main_path.write_text(json.dumps(good, ensure_ascii=False, indent=1), encoding="utf-8")
    review_path.write_text(json.dumps(needs_review, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.llm:
        llm_path.write_text(json.dumps(assisted, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"parsed {len(questions)} question blocks, answer key entries: {len(key)}")
    if key_conflicts:
        print(f"WARNING: conflicting key entries: {key_conflicts}")
    if missing:
        print(f"WARNING: question numbers never matched: {missing}")
    print(f"  clean rows          -> {main_path} ({len(good)})")
    print(f"  needs review        -> {review_path} ({len(needs_review)})")
    if args.llm:
        ok = sum(1 for e in assisted if e["status"] == "ok")
        print(f"  llm-assisted        -> {llm_path} ({ok} ok / {len(assisted)} touched)")
    print("STOP: review the output, then re-run with --insert "
          "(and, after full manual review, --insert-llm-assisted).")


if __name__ == "__main__":
    main()
