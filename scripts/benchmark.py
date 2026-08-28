#!/usr/bin/env python3
"""Measure what this hook would save, on your PDFs rather than mine.

The README quotes numbers from 35 public PDFs (scripts/corpus.json). Yours are
almost certainly different: the saving scales with how little text each page
carries, so a slide deck and a journal article sit at opposite ends. Run this
over a directory and you get your own table.

  python scripts/benchmark.py ~/Documents/papers
  python scripts/benchmark.py file1.pdf file2.pdf --csv out.csv
  python scripts/benchmark.py corpus --manifest scripts/corpus.json

Nothing is written unless you pass --csv, and no PDF is ever modified.

Both token columns are estimates, not billing records:
  text   CJK characters counted at 1.3 per token, everything else at 4,
         over the extracted text after stripping control characters
  image  whatever hooks/pdf_text_router.py's est_image_tokens() returns,
         so the two stay in step (28-px grid, long edge and per-image caps)

"scan" vs "text" is the hook's own verdict (classify()): fewer than
MIN_TOTAL_CHARS characters overall, or more than half the pages under
BLANK_PAGE_CHARS characters, and the file goes to vision.

The image column follows whichever path Claude Code would actually take for
that file, because the two paths do not cost the same:
  <= 10 pages   API document block: an image *and* the text of every page
  > 10 pages    poppler: pdftoppm -jpeg -r 100, images only, no text

With --manifest the rows are grouped by the manifest's "category" field and
the two paths are printed as separate per-file tables, followed by the
break-even density for the poppler path.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "hooks"))
import pdf_text_router as R          # noqa: E402

# Long-edge and per-image caps come from the hook's est_image_tokens()
# defaults (IMAGE_LONG_EDGE_CAP / IMAGE_TOKEN_CAP), so they cannot drift.
POPPLER_DPI = 100                    # Claude Code runs pdftoppm -r 100
LETTER_PT = (612.0, 792.0)


def collect(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                out += [os.path.join(root, f) for f in sorted(files)
                        if f.lower().endswith(".pdf")]
        elif p.lower().endswith(".pdf"):
            out.append(p)
    return out


def load_manifest(path):
    """local file name -> category, using the same naming as fetch_corpus.py"""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {e["id"].replace("/", "__") + ".pdf": e.get("category", "")
            for e in data.get("files", [])}


def measure(path):
    engine = R.open_pdf(path)
    if engine is None:
        return {"file": path, "error": "unreadable"}
    try:
        pages = engine.page_count()
        if not pages:
            return {"file": path, "error": "no pages"}
        # Same inspection and verdict the hook itself uses, so a file that
        # the benchmark calls a scan is one the hook would send to vision.
        meta, texts = R.inspect_pdf(engine)
        _, scan = R.classify(meta)
        body = "".join(texts)
        chars = meta["chars"]
        cjk = sum(1 for c in body if R.is_cjk(c))
        w, h = meta["w_pt"], meta["h_pt"]
        text_tokens = meta["text_tokens"]
        if pages > R.NATIVE_WHOLE_FILE_LIMIT:
            img_tokens = R.est_image_tokens(w, h, pages, dpi=POPPLER_DPI)
            route = "poppler"
        else:
            img_tokens = R.est_image_tokens(w, h, pages) + text_tokens
            route = "api"
        return {
            "file": path, "pages": pages, "chars": chars,
            "cjk_pct": (100.0 * cjk / chars) if chars else 0.0,
            "chars_per_page": chars / float(pages),
            "text_tokens": text_tokens, "image_tokens": img_tokens,
            "ratio": (img_tokens / float(text_tokens)) if text_tokens else 0.0,
            "route": route, "kind": "scan" if scan else "text",
        }
    finally:
        engine.close()


def label_of(r, i, anonymize):
    return "doc-%02d" % i if anonymize else os.path.basename(r["file"])[:38]


def pooled(rows):
    img = sum(r["image_tokens"] for r in rows)
    txt = sum(r["text_tokens"] for r in rows)
    return img, txt, (img / float(txt)) if txt else 0.0


def print_by_category(rows):
    cats = {}
    for r in rows:
        cats.setdefault(r["category"] or "(uncategorised)", []).append(r)
    print("\nBy category (pooled over files with a text layer; scans listed "
          "but not pooled):")
    print("  %-32s %5s %5s %9s %9s %6s"
          % ("category", "text", "scan", "img tok", "text tok", "ratio"))
    for cat in sorted(cats):
        texty = [r for r in cats[cat] if r["kind"] == "text"]
        scans = [r for r in cats[cat] if r["kind"] == "scan"]
        img, txt, ratio = pooled(texty)
        print("  %-32s %5d %5d %9s %9s %5.2fx"
              % (cat[:32], len(texty), len(scans), format(img, ","),
                 format(txt, ","), ratio))


def print_by_route(rows, anonymize):
    texty = [r for r in rows if r["kind"] == "text"]
    for route, title in (("api", "Document block path (<= %d pages): image "
                                 "and text of every page are both billed"
                                 % R.NATIVE_WHOLE_FILE_LIMIT),
                         ("poppler", "Poppler path (> %d pages): pdftoppm at "
                                     "%d DPI, images only"
                                     % (R.NATIVE_WHOLE_FILE_LIMIT,
                                        POPPLER_DPI))):
        sub = sorted((r for r in texty if r["route"] == route),
                     key=lambda r: -r["ratio"])
        if not sub:
            continue
        print("\n%s" % title)
        print("  %-38s %5s %9s %9s %9s %6s"
              % ("file", "pages", "chars/pg", "img tok", "text tok", "ratio"))
        for i, r in enumerate(sub, 1):
            print("  %-38s %5d %9s %9s %9s %5.2fx"
                  % (label_of(r, i, anonymize), r["pages"],
                     format(int(r["chars_per_page"]), ","),
                     format(r["image_tokens"], ","),
                     format(r["text_tokens"], ","), r["ratio"]))
        img, txt, ratio = pooled(sub)
        print("  %-38s %5s %9s %9s %9s %5.2fx"
              % ("pooled (%d files)" % len(sub), "", "",
                 format(img, ","), format(txt, ","), ratio))


def print_break_even():
    per_page = R.est_image_tokens(LETTER_PT[0], LETTER_PT[1], 1,
                                  dpi=POPPLER_DPI)
    print("\nBreak-even on the poppler path: one letter-size page at %d DPI "
          "is ~%s image tokens, so text only costs more above ~%s Latin "
          "characters or ~%s CJK characters per page. The document block "
          "path bills text as well as the image, so extracting the text "
          "there is never a loss."
          % (POPPLER_DPI, format(per_page, ","),
             format(int(round(per_page * 4.0, -2)), ","),
             format(int(round(per_page * 1.3, -2)), ",")))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", help="PDF files or directories")
    ap.add_argument("--csv", help="also write the rows to this file")
    ap.add_argument("--manifest",
                    help="scripts/corpus.json-style file: adds a category "
                         "column and per-category / per-route tables")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    ap.add_argument("--anonymize", action="store_true",
                    help="print doc-01, doc-02 ... instead of real filenames, "
                         "so the output is safe to paste into an issue")
    args = ap.parse_args()

    files = collect(args.paths)
    if not files:
        print("no PDFs found")
        return 1
    categories = load_manifest(args.manifest) if args.manifest else {}

    rows, errors = [], []
    for path in files:
        try:
            row = measure(path)
        except Exception as exc:
            errors.append((path, str(exc)[:60]))
            continue
        if "error" in row:
            errors.append((path, row["error"]))
            continue
        row["category"] = categories.get(os.path.basename(path), "")
        rows.append(row)

    if not args.quiet:
        print("%-38s %5s %8s %5s %5s %9s %9s %6s"
              % ("file", "pages", "chars", "cjk%", "kind", "text tok",
                 "img tok", "ratio"))
        print("-" * 96)
        for i, r in enumerate(rows, 1):
            print("%-38s %5d %8d %5.0f %5s %9s %9s %5.2fx" % (
                label_of(r, i, args.anonymize), r["pages"], r["chars"],
                r["cjk_pct"], r["kind"], format(r["text_tokens"], ","),
                format(r["image_tokens"], ","), r["ratio"]))

    texty = [r for r in rows if r["kind"] == "text"]
    scans = [r for r in rows if r["kind"] == "scan"]
    print("\n%d PDFs: %d with a text layer, %d scans, %d unreadable"
          % (len(rows) + len(errors), len(texty), len(scans), len(errors)))
    for i, (path, why) in enumerate(errors, 1):
        print("  unreadable: %s (%s)"
              % ("bad-%02d" % i if args.anonymize
                 else os.path.basename(path), why))

    if texty:
        img, txt, ratio = pooled(texty)
        saved = img - txt
        ratios = sorted(r["ratio"] for r in texty)
        mid = ratios[len(ratios) // 2]
        print("\nOn the %d with a text layer:" % len(texty))
        print("  pooled      ~%s image tokens vs ~%s text tokens (%.2fx)"
              % (format(img, ","), format(txt, ","), ratio))
        print("  per file    best %.2fx, median %.2fx, worst %.2fx"
              % (ratios[-1], mid, ratios[0]))
        print("  saved       ~%s tokens across the set" % format(saved, ","))
        thin = [r for r in texty if r["chars_per_page"] < 1500]
        dense = [r for r in texty if r["chars_per_page"] >= 1500]
        if thin and dense:
            tr = sum(r["ratio"] for r in thin) / len(thin)
            dr = sum(r["ratio"] for r in dense) / len(dense)
            print("  by density  %.2fx on %d sparse pages (<1500 chars/page), "
                  "%.2fx on %d dense ones" % (tr, len(thin), dr, len(dense)))
    if scans:
        print("\n%d scans would go to vision either way; above %d pages they "
              "need poppler, and this hook renders them locally instead."
              % (len(scans), R.NATIVE_WHOLE_FILE_LIMIT))

    if args.manifest and rows:
        print_by_category(rows)
        print_by_route(rows, args.anonymize)
        print_break_even()

    if args.csv:
        import csv
        cols = ["file", "category", "pages", "chars", "cjk_pct",
                "chars_per_page", "text_tokens", "image_tokens", "ratio",
                "route", "kind"]
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)), exist_ok=True)
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols)
            wr.writeheader()
            for i, r in enumerate(rows, 1):
                out = {c: r[c] for c in cols}
                out["file"] = (label_of(r, i, args.anonymize)
                               if args.anonymize else os.path.basename(r["file"]))
                wr.writerow(out)
        print("\nwrote %s" % args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
