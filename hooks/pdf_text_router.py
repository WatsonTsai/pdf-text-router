#!/usr/bin/env python3
"""
pdf-text-router - a Claude Code PreToolUse hook for the Read tool.

Claude Code reads a PDF by rendering its pages to images. That is the right
call for a scan and the wrong call for anything with a real text layer, and
for PDFs longer than ten pages it needs poppler's `pdftoppm`, an external
binary that the official setup docs never mention.

This hook routes each Read on a .pdf to whichever path actually works:

  text layer present   -> deny, extract to UTF-8 .txt, tell Claude to read that
  scan, native path ok -> allow, Claude's own vision is the OCR engine
  scan, native broken  -> deny, render pages here, tell Claude to read the PNGs
  anything unexpected  -> allow (fail-open; a hook must never block a Read)

Contract: no stdout + exit 0 means "proceed". Printing JSON means "decide".

CLI:
  python pdf_text_router.py --selftest        environment report
  python pdf_text_router.py --check FILE.pdf  dry-run one PDF, no side effects
"""

import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata

# --- tunables ---------------------------------------------------------------

CACHE_DIR = os.environ.get("PDF_TEXT_ROUTER_CACHE") or os.path.join(
    os.path.expanduser("~"), ".claude", "pdf-text-cache")

MIN_TOTAL_CHARS = 50       # whole document below this -> treat as a scan
MIN_CHARS_PER_PAGE = 15    # per-page average below this -> treat as a scan
VISUAL_PAGE_LIMIT = 3      # an explicit range this short means "I want to look"
NATIVE_WHOLE_FILE_LIMIT = 10   # Claude Code reads <= this many pages poppler-free
RENDER_SCALE = 1.5         # ~108 DPI; legible for OCR without doubling tokens
MAX_RENDER_PAGES = 5       # never hand back more rendered pages than this
LARGE_TEXT_TOKENS = 40000  # above this, tell Claude to Grep instead of Read
MAX_READ_LINES = 1800      # Read truncates around 2000 lines; stay under it

# --- hook I/O ---------------------------------------------------------------


def allow():
    """No output + exit 0: let the Read proceed untouched."""
    sys.exit(0)


def deny(reason, sysmsg=None):
    out = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}
    if sysmsg:
        out["systemMessage"] = sysmsg
    # ensure_ascii keeps the payload valid under any console encoding (cp950...)
    sys.stdout.write(json.dumps(out, ensure_ascii=True))
    sys.exit(0)


# --- pdf engines ------------------------------------------------------------
# Preference order is deliberate:
#   pypdfium2  BSD-3/Apache-2.0, prebuilt wheels everywhere, extracts and renders
#   pymupdf    best-in-class but AGPL-3.0, so only used if already installed
#   pypdf      pure-python fallback; extracts only, and drops glyphs on complex
#              layouts (measured: -7.7% CJK characters on a two-column report)


class Engine(object):
    name = None
    can_render = False

    def __init__(self, path):
        self.path = path

    def close(self):
        pass


class Pdfium(Engine):
    name = "pypdfium2"
    can_render = True

    def __init__(self, path):
        import pypdfium2
        self._m = pypdfium2
        self.doc = pypdfium2.PdfDocument(path)
        Engine.__init__(self, path)

    @property
    def encrypted(self):
        # pdfium raises on load for password-protected files, so reaching here
        # means the document opened fine.
        return False

    def page_count(self):
        return len(self.doc)

    def texts(self):
        out = []
        for page in self.doc:
            out.append(page.get_textpage().get_text_bounded())
        return out

    def page_size_pt(self, i):
        return self.doc[i].get_size()

    def render(self, index, scale, dest):
        self.doc[index].render(scale=scale).to_pil().save(dest)
        return dest

    def close(self):
        # Without this, pdfium keeps the file open and Windows refuses to
        # delete or overwrite the PDF for the rest of the process.
        self.doc.close()


class MuPdf(Engine):
    name = "pymupdf"
    can_render = True

    def __init__(self, path):
        import fitz
        self._m = fitz
        self.doc = fitz.open(path)
        Engine.__init__(self, path)

    @property
    def encrypted(self):
        return bool(self.doc.is_encrypted or self.doc.needs_pass)

    def page_count(self):
        return self.doc.page_count

    def texts(self):
        return [p.get_text() for p in self.doc]

    def page_size_pt(self, i):
        r = self.doc[i].rect
        return (r.width, r.height)

    def render(self, index, scale, dest):
        m = self._m.Matrix(scale, scale)
        self.doc[index].get_pixmap(matrix=m).save(dest)
        return dest

    def close(self):
        self.doc.close()


class PyPdf(Engine):
    name = "pypdf"
    can_render = False

    def __init__(self, path):
        import pypdf
        self.reader = pypdf.PdfReader(path)
        Engine.__init__(self, path)

    @property
    def encrypted(self):
        return bool(self.reader.is_encrypted)

    def page_count(self):
        return len(self.reader.pages)

    def texts(self):
        return [p.extract_text() or "" for p in self.reader.pages]

    def page_size_pt(self, i):
        box = self.reader.pages[i].mediabox
        return (float(box.width), float(box.height))

    def render(self, index, scale, dest):
        raise NotImplementedError


ENGINES = (Pdfium, MuPdf, PyPdf)
MODULE_OF = {"pypdfium2": "pypdfium2", "pymupdf": "fitz", "pypdf": "pypdf"}


def open_pdf(path):
    """First engine that imports and opens the file wins. None if all fail."""
    for cls in ENGINES:
        try:
            return cls(path)
        except ImportError:
            continue
        except Exception:
            # A real parse failure: let Read report it in its own words.
            return None
    return None


# --- estimation -------------------------------------------------------------


def is_cjk(ch):
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF
            or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFF60)


def est_text_tokens(text):
    """Latin runs ~4 chars/token; CJK runs closer to 1.3. Using one ratio for
    both is why the previous version undercounted Chinese by 2-3x."""
    cjk = 0
    for ch in text:
        if is_cjk(ch):
            cjk += 1
    return int(cjk / 1.3 + (len(text) - cjk) / 4.0)


def est_image_tokens(w_pt, h_pt, pages, dpi=None, cap=None):
    """Anthropic bills an image at roughly (width * height) / 750 tokens."""
    w, h = abs(w_pt), abs(h_pt)
    if dpi:
        w, h = w / 72.0 * dpi, h / 72.0 * dpi
    longest = max(w, h)
    if cap and longest > cap:
        k = cap / longest
        w, h = w * k, h * k
    # Round once at the end: rounding per page loses ~1% over a long document.
    return int(w * h / 750.0 * max(pages, 0))


def has_poppler():
    return bool(shutil.which("pdftoppm"))


# --- helpers ----------------------------------------------------------------


def span_of(pages):
    """'1-5' -> 5, '3' -> 1, anything malformed -> None."""
    if not isinstance(pages, str):
        return None
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", pages)
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return max(b - a + 1, 1)


def first_page_of(pages):
    m = re.match(r"\s*(\d+)", pages or "")
    return int(m.group(1)) if m else 1


def clean(text):
    """Normalise line endings and strip control characters that survive
    extraction (pdfium emits a couple per document on some files)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        c for c in text
        if c in "\n\t" or not unicodedata.category(c).startswith("C"))


SAMPLE_BYTES = 262144   # hash the ends of very large files rather than all of it


def cache_key(path):
    """Keyed on content, not on stat().

    size + mtime looks sufficient until you regenerate a PDF: exporting the
    same document twice often lands in the same second at the same byte
    count, and a stat-based key then serves the previous version's text while
    the real Read stays denied, so nobody can tell.
    """
    st = os.stat(path)
    h = hashlib.sha1()
    h.update(("%d|" % st.st_size).encode("ascii"))
    with open(path, "rb") as fh:
        if st.st_size <= SAMPLE_BYTES * 2:
            h.update(fh.read())
        else:
            h.update(fh.read(SAMPLE_BYTES))
            fh.seek(-SAMPLE_BYTES, os.SEEK_END)
            h.update(fh.read(SAMPLE_BYTES))
    return h.hexdigest()[:12]


def safe_name(path, limit=60):
    return re.sub(r"[^\w\-.]", "_", os.path.basename(path))[:limit]


def text_cache_path(path):
    return os.path.join(CACHE_DIR,
                        "%s-%s.txt" % (cache_key(path), safe_name(path)))


def image_cache_path(path, page, scale):
    return os.path.join(CACHE_DIR, "%s-p%d-%.1fx-%s.png" % (
        cache_key(path), page, scale, safe_name(path, 40)))


TEXT_HEADER = "# Text extracted from:"


def count_lines(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


def text_cache_is_complete(path):
    """A cache entry is only trustworthy if it was finished.

    Claude Code kills a hook that overruns its timeout, so a half-written
    file is a normal event, not an exotic one -- and a truncated extraction
    is worse than no extraction, because Claude reads the prefix and reports
    on it as though it were the document.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
        return head.startswith(TEXT_HEADER) and "===== [page 1/" in head
    except OSError:
        return False


def png_is_complete(path):
    """A PNG ends with the IEND chunk: 4 length bytes, b'IEND', 4 CRC bytes.
    A render that was killed halfway does not."""
    try:
        if os.path.getsize(path) < 24:
            return False
        with open(path, "rb") as fh:
            fh.seek(-8, os.SEEK_END)
            return fh.read(4) == b"IEND"
    except OSError:
        return False


def write_text_cache(dest, src, pages_text):
    os.makedirs(CACHE_DIR, exist_ok=True)
    total = sum(len(t) for t in pages_text)
    parts = ["%s %s\n# %d pages, %d chars (text layer, no OCR needed)\n"
             % (TEXT_HEADER, src, len(pages_text), total)]
    for i, t in enumerate(pages_text, 1):
        parts.append("\n\n===== [page %d/%d] =====\n\n%s"
                     % (i, len(pages_text), t.rstrip()))
    tmp = "%s.tmp%d" % (dest, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
    os.replace(tmp, dest)     # atomic: readers see the old file or the new one
    return count_lines(dest)


def render_pages(engine, path, first, count, scale=RENDER_SCALE):
    """Render a page range here so the caller never needs poppler."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = []
    last = min(first + count - 1, engine.page_count())
    for page in range(max(first, 1), last + 1):
        dest = image_cache_path(path, page, scale)
        if not (os.path.isfile(dest) and png_is_complete(dest)):
            tmp = "%s.tmp%d.png" % (dest[:-4], os.getpid())
            engine.render(page - 1, scale, tmp)
            os.replace(tmp, dest)
        out.append((page, dest))
    return out


# --- decision ---------------------------------------------------------------


def decide(file_path, pages_arg, dry_run=False):
    """Returns (action, payload). Side-effect free when dry_run is set."""
    engine = open_pdf(file_path)
    if engine is None:
        return ("allow", "no usable pdf engine, or the file failed to parse")

    try:
        if engine.encrypted:
            return ("allow", "encrypted; let Read report that itself")

        n = engine.page_count()
        if n == 0:
            return ("allow", "zero pages")

        pages_text = [clean(t) for t in engine.texts()]
        total = sum(len(t.strip()) for t in pages_text)
        per_page = total / float(n)
        scanned = total < MIN_TOTAL_CHARS or per_page < MIN_CHARS_PER_PAGE

        span = span_of(pages_arg)
        wants_layout = span is not None and span <= VISUAL_PAGE_LIMIT
        # Claude Code only shells out to pdftoppm when a page range is involved;
        # a short whole-file read goes straight to the API as a document block.
        native_needs_poppler = (span is not None) or (n > NATIVE_WHOLE_FILE_LIMIT)
        native_works = has_poppler() or not native_needs_poppler

        if scanned or wants_layout:
            why = "scan" if scanned else "explicit short range"
            if native_works:
                return ("allow", "%s; native render path is available" % why)
            if not engine.can_render:
                return ("allow", "%s; %s cannot render" % (why, engine.name))
            first = first_page_of(pages_arg) if span else 1
            count = min(span or MAX_RENDER_PAGES, MAX_RENDER_PAGES)
            if first > n or first < 1:
                # Out of range, and the native path that would have reported
                # that is exactly the one missing poppler. Say it ourselves.
                return ("outofrange", (first, n))
            if dry_run:
                return ("render", (first, count, n, scanned, None))
            imgs = render_pages(engine, file_path, first, count)
            if not imgs:
                return ("outofrange", (first, n))
            return ("render", (first, count, n, scanned, imgs))

        w_pt, h_pt = engine.page_size_pt(0)
        text_tokens = est_text_tokens("".join(pages_text))
        if native_needs_poppler:
            # poppler path: pdftoppm -jpeg -r 100, images only, no text layer.
            # The JPEG still goes to the API, which downscales anything over
            # 1568px on its long edge before billing it.
            # Clamp to the real page count: an absurd range like "1-999999"
            # would otherwise put an astronomical number in front of Claude.
            img_tokens = est_image_tokens(w_pt, h_pt, min(span or n, n),
                                          dpi=100, cap=1568)
            path_note = "poppler path (100 DPI JPEG, no text layer attached)"
        else:
            # API document block: every page as an image *and* its text
            img_tokens = est_image_tokens(w_pt, h_pt, n, cap=1568) + text_tokens
            path_note = "API document block (an image plus the text of every page)"

        if dry_run:
            return ("text", (n, total, text_tokens, img_tokens, path_note,
                             native_works, engine.name, None, None))

        dest = text_cache_path(file_path)
        if os.path.isfile(dest) and text_cache_is_complete(dest):
            lines = count_lines(dest)
        else:
            lines = write_text_cache(dest, file_path, pages_text)
        return ("text", (n, total, text_tokens, img_tokens, path_note,
                         native_works, engine.name, dest, lines))
    finally:
        engine.close()


# --- messages ---------------------------------------------------------------


def render_message(first, count, n, scanned, imgs):
    listing = "\n".join("  page %d -> %s" % (p, f) for p, f in imgs)
    if scanned:
        why = ("This PDF has no text layer, so it has to be looked at rather than "
               "parsed")
    else:
        why = ("You asked for a short page range, which means you want to see the "
               "layout rather than read the words")
    return (
        "{why} -- but Claude Code's page-render path shells out to poppler's "
        "`pdftoppm`, which is not on PATH here, so that Read would have failed.\n\n"
        "{k} of {n} page(s) have been rendered locally instead:\n{listing}\n\n"
        "ACTION: call Read on those .png paths. Your vision is the OCR engine; the "
        "images are all you need.\n\n"
        "For other pages, ask for another range and this hook will render it the "
        "same way.".format(why=why, k=len(imgs), n=n, listing=listing))


def text_message(n, total, text_tokens, img_tokens, path_note, native_works,
                 dest, lines):
    saved = max(img_tokens - text_tokens, 0)
    if native_works:
        head = ("This PDF has a real text layer ({n} pages, {c:,} chars). Reading it "
                "as pages goes through the {note}, an estimated ~{i:,} tokens against "
                "~{t:,} for the text itself -- roughly {s:,} spent on pictures of "
                "words.".format(n=n, c=total, note=path_note, i=img_tokens,
                                t=text_tokens, s=saved))
    else:
        head = ("This PDF has a real text layer ({n} pages, {c:,} chars), and the "
                "page-render path is unavailable anyway: it needs poppler's "
                "`pdftoppm`, which is not on PATH here. Extracting the text is not "
                "merely cheaper (~{t:,} tokens against ~{i:,}), it is the only path "
                "that works.".format(n=n, c=total, t=text_tokens, i=img_tokens))

    if text_tokens > LARGE_TEXT_TOKENS or lines > MAX_READ_LINES:
        why = ("longer than Read shows in one call" if lines > MAX_READ_LINES
               else "large")
        body = ("\n\nThe text is {w} ({l:,} lines, ~{t:,} tokens), so reading it "
                "whole would trade one context problem for another -- Read stops "
                "around 2000 lines and would hand you a prefix without saying so. "
                "It is extracted to UTF-8 here:\n  {d}\n\n"
                "ACTION: Grep that file for what you need, or Read it with "
                "offset/limit. It carries [page N/M] markers, so you can still cite "
                "page numbers.".format(w=why, t=text_tokens, l=lines, d=dest))
    else:
        body = ("\n\nThe full text is extracted to UTF-8 here ({l:,} lines):\n  {d}\n\n"
                "ACTION: call Read on that .txt path. It carries [page N/M] markers, "
                "so you can still cite page numbers.".format(l=lines, d=dest))

    if native_works:
        body += ("\n\nIf you specifically need to SEE a figure, a table or the "
                 "layout, re-run Read on the .pdf with a range of {v} pages or fewer "
                 "(e.g. pages=\"4-5\") and this hook will let it render."
                 .format(v=VISUAL_PAGE_LIMIT))
    else:
        body += ("\n\nIf you specifically need to SEE a figure or the layout, re-run "
                 "Read on the .pdf with a range of {v} pages or fewer "
                 "(e.g. pages=\"4-5\"); this hook will render those pages to PNG "
                 "locally, since the native path cannot.".format(v=VISUAL_PAGE_LIMIT))
    return head + body


def main():
    data = json.loads(sys.stdin.read() or "{}")
    if data.get("tool_name") not in (None, "Read"):
        allow()
    ti = data.get("tool_input") or {}
    fp = ti.get("file_path") or ""
    if not fp.lower().endswith(".pdf") or not os.path.isfile(fp):
        allow()

    action, payload = decide(fp, ti.get("pages"))

    if action == "allow":
        allow()

    if action == "outofrange":
        first, n = payload
        deny("This PDF has {n} page(s), so the range you asked for does not "
             "exist. Normally Read would tell you that, but on this machine "
             "the page-range path needs poppler's `pdftoppm`, which is not "
             "installed, so you would have got an unrelated error about "
             "poppler instead.\n\n"
             "ACTION: re-run Read with a range inside 1-{n}, or with no "
             "`pages` argument at all.".format(n=n),
             sysmsg="Requested page %d is outside 1-%d" % (first, n))

    if action == "render":
        first, count, n, scanned, imgs = payload
        deny(render_message(first, count, n, scanned, imgs),
             sysmsg="No poppler: rendered %d page(s) locally" % len(imgs))

    n, total, tt, it, note, native, eng, dest, lines = payload
    deny(text_message(n, total, tt, it, note, native, dest, lines),
         sysmsg="PDF text layer via %s -- reading text, ~%s tokens instead of ~%s"
                % (eng, format(tt, ","), format(it, ",")))


# --- cli --------------------------------------------------------------------


def selftest():
    print("pdf-text-router self-test")
    print("  python           %s" % sys.version.split()[0])
    found = []
    for cls in ENGINES:
        try:
            __import__(MODULE_OF[cls.name])
            found.append(cls)
            print("  engine           %-10s available%s"
                  % (cls.name, "" if cls.can_render else "  (extract only)"))
        except ImportError:
            print("  engine           %-10s not installed" % cls.name)
    if not found:
        print("\n  FAIL: no PDF engine. Run `pip install pypdfium2` and try again.")
        return 1

    active = found[0]
    print("  active engine    %s" % active.name)
    if active.can_render:
        if active is Pdfium:
            try:
                __import__("PIL")
                print("  rendering        ready")
            except ImportError:
                print("  rendering        UNAVAILABLE: pypdfium2 needs Pillow to "
                      "write PNGs (`pip install pillow`)")
        else:
            print("  rendering        ready")
    else:
        print("  rendering        unavailable with %s (extract only)" % active.name)

    where = shutil.which("pdftoppm")
    print("  poppler pdftoppm %s" % (
        where if where else
        "not on PATH -- PDFs over %d pages, and any read with a page range, "
        "cannot be rendered natively; this hook is their only working path"
        % NATIVE_WHOLE_FILE_LIMIT))
    print("  cache dir        %s" % CACHE_DIR)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        probe = os.path.join(CACHE_DIR, ".write-probe")
        with open(probe, "w") as fh:
            fh.write("")
        os.remove(probe)
        print("  cache writable   yes")
    except Exception as exc:
        print("  cache writable   NO: %s" % exc)
        return 1
    print("\n  OK. Run --check on a real PDF to see a routing decision.")
    return 0


def check(path):
    if not path or not os.path.isfile(path):
        print("no such file: %s" % path)
        return 1
    action, payload = decide(path, None, dry_run=True)
    print("%s\n  decision       %s" % (path, action))
    if action == "allow":
        print("  reason         %s" % payload)
    elif action == "outofrange":
        print("  reason         requested page %d, file has %d" % payload)
    elif action == "render":
        first, count, n, scanned, _ = payload
        print("  reason         %s, and poppler is missing"
              % ("no text layer" if scanned else "short explicit range"))
        print("  would render   pages %d-%d of %d"
              % (first, min(first + count - 1, n), n))
    else:
        n, total, tt, it, note, native, eng, _, _ = payload
        print("  engine         %s" % eng)
        print("  pages / chars  %d / %d" % (n, total))
        print("  native path    %s (%s)" % ("works" if native else "BROKEN", note))
        print("  text tokens    ~%s" % format(tt, ","))
        print("  render tokens  ~%s" % format(it, ","))
        print("  ratio          %s" % ("%.2fx" % (it / float(tt)) if tt else "n/a"))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--check" in sys.argv:
        i = sys.argv.index("--check")
        sys.exit(check(sys.argv[i + 1] if len(sys.argv) > i + 1 else ""))
    try:
        main()
    except Exception:
        allow()   # fail-open: nothing here is worth blocking a Read over
