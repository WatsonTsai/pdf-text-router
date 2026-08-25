#!/usr/bin/env python3
"""Measure what this hook would save, on your PDFs rather than mine.

The README quotes numbers from one person's document collection. Yours are
almost certainly different: the saving scales with how little text each page
carries, so a slide deck and a journal article sit at opposite ends. Run this
over a directory and you get your own table.

  python scripts/benchmark.py ~/Documents/papers
  python scripts/benchmark.py file1.pdf file2.pdf --csv out.csv

Nothing is written unless you pass --csv, and no PDF is ever modified.

Both token columns are estimates, not billing records:
  text   CJK characters counted at 1.3 per token, everything else at 4
  image  (width * height) / 750, the ratio Anthropic documents for images

The image column follows whichever path Claude Code would actually take for
that file, because the two paths do not cost the same:
  <= 10 pages   API document block: an image *and* the text of every page
  > 10 pages    poppler: pdftoppm -jpeg -r 100, images only, no text
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "hooks"))
import pdf_text_router as R          # noqa: E402


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


def measure(path):
    engine = R.open_pdf(path)
    if engine is None:
        return {"file": path, "error": "unreadable"}
    try:
        pages = engine.page_count()
        if not pages:
            return {"file": path, "error": "no pages"}
        texts = [R.clean(t) for t in engine.texts()]
        body = "".join(texts)
        chars = len(body.strip())
        cjk = sum(1 for c in body if R.is_cjk(c))
        w, h = engine.page_size_pt(0)
        text_tokens = R.est_text_tokens(body)
        if pages > R.NATIVE_WHOLE_FILE_LIMIT:
            # pdftoppm renders at 100 DPI, but the JPEG still goes to the API,
            # which downscales past 1568px on the long edge before billing.
            img_tokens = R.est_image_tokens(w, h, pages, dpi=100, cap=1568)
            route = "poppler"
        else:
            img_tokens = R.est_image_tokens(w, h, pages, cap=1568) + text_tokens
            route = "api"
        scan = (chars < R.MIN_TOTAL_CHARS
                or chars / float(pages) < R.MIN_CHARS_PER_PAGE)
        return {
            "file": path, "pages": pages, "chars": chars,
            "cjk_pct": (100.0 * cjk / len(body)) if body else 0.0,
            "chars_per_page": chars / float(pages),
            "text_tokens": text_tokens, "image_tokens": img_tokens,
            "ratio": (img_tokens / float(text_tokens)) if text_tokens else 0.0,
            "route": route, "kind": "scan" if scan else "text",
        }
    finally:
        engine.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", help="PDF files or directories")
    ap.add_argument("--csv", help="also write the rows to this file")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    ap.add_argument("--anonymize", action="store_true",
                    help="print doc-01, doc-02 ... instead of real filenames, "
                         "so the output is safe to paste into an issue")
    args = ap.parse_args()

    files = collect(args.paths)
    if not files:
        print("no PDFs found")
        return 1

    rows, errors = [], []
    for path in files:
        try:
            row = measure(path)
        except Exception as exc:
            errors.append((path, str(exc)[:60]))
            continue
        (errors if "error" in row else rows).append(
            (path, row["error"]) if "error" in row else row)

    if not args.quiet:
        print("%-38s %5s %8s %5s %5s %9s %9s %6s"
              % ("file", "pages", "chars", "cjk%", "kind", "text tok",
                 "img tok", "ratio"))
        print("-" * 96)
        for i, r in enumerate(rows, 1):
            label = ("doc-%02d" % i if args.anonymize
                     else os.path.basename(r["file"])[:38])
            print("%-38s %5d %8d %5.0f %5s %9s %9s %5.2fx" % (
                label, r["pages"], r["chars"],
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
        saved = sum(r["image_tokens"] - r["text_tokens"] for r in texty)
        img = sum(r["image_tokens"] for r in texty)
        txt = sum(r["text_tokens"] for r in texty)
        ratios = sorted(r["ratio"] for r in texty)
        mid = ratios[len(ratios) // 2]
        print("\nOn the %d with a text layer:" % len(texty))
        print("  pooled      ~%s image tokens vs ~%s text tokens (%.2fx)"
              % (format(img, ","), format(txt, ","), img / float(txt)))
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
        print("\n%d scans would go to vision either way; above 10 pages they "
              "need poppler, and this hook renders them locally instead."
              % len(scans))

    if args.csv:
        import csv
        cols = ["file", "pages", "chars", "cjk_pct", "chars_per_page",
                "text_tokens", "image_tokens", "ratio", "route", "kind"]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols)
            wr.writeheader()
            for r in rows:
                wr.writerow({c: r[c] for c in cols})
        print("\nwrote %s" % args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
