#!/usr/bin/env python3
"""
pdf-text-router - a Claude Code PreToolUse hook for the Read tool.

Claude Code reads a PDF by rendering its pages to images. That is the right
call for a scan and the wrong call for anything with a real text layer, and
for PDFs longer than ten pages it needs poppler's `pdftoppm`, an external
binary that the official setup docs never mention.

This hook routes each Read on a .pdf to whichever path actually works:

  text layer present   -> extract to UTF-8 .txt and point the Read at it
  scan, native path ok -> allow, Claude's own vision is the OCR engine
  scan, native broken  -> render pages here and point the Read at the PNGs
  bad `pages` argument -> deny with the real reason (nothing to redirect to)
  anything unexpected  -> allow (fail-open; a hook must never block a Read)

"Point the Read at" depends on PDF_TEXT_ROUTER_MODE:

  rewrite (default)  allow + updatedInput, so the same Read call returns the
                     .txt or the .png, with additionalContext explaining what
                     happened. One tool call instead of two.
  deny               deny with an ACTION message asking for a second Read on
                     the new path. Use this on Claude Code versions that do
                     not honour updatedInput.

Per-page classification, from the character count and the share of the page
covered by image objects (pdfium and MuPDF report that; pypdf does not):

  blank    < BLANK_PAGE_CHARS chars (before and after clean()),
           < IMAGE_PAGE_MIN_AREA image                              nothing there
  image    < BLANK_PAGE_CHARS chars, image present or a text layer
           clean() stripped to nothing (symbol fonts)               scan or picture
  figure   < FIGURE_PAGE_CHARS chars, >= FIGURE_PAGE_IMAGE_AREA     mostly a picture
  text     everything else

A document is a scan when it has fewer than MIN_TOTAL_CHARS characters overall
or when more than half of its pages are blank or image pages. In a text
document each non-text class is listed separately, so Claude skips blank
pages and looks at image and figure pages instead of guessing.

Contract: no stdout + exit 0 means "proceed". Printing JSON means "decide".

CLI:
  python pdf_text_router.py --selftest        environment report
  python pdf_text_router.py --check FILE.pdf  dry-run one PDF, no side effects
  python pdf_text_router.py --clear-cache     delete every cached .txt/.png

Environment:
  PDF_TEXT_ROUTER_MODE          rewrite | deny             (default rewrite)
  PDF_TEXT_ROUTER_CACHE         cache directory            (~/.claude/pdf-text-cache)
                                or the word `beside`: write <name>.pdf.txt,
                                .json and .p<N>.<scale>x.png next to the PDF itself
  PDF_TEXT_ROUTER_CACHE_MAX_MB  prune above this size      (default 500)
  PDF_TEXT_ROUTER_SCALE         render scale, 1.0 to 3.0   (default 1.5)
"""

import collections
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import unicodedata

# --- tunables ---------------------------------------------------------------

DEFAULT_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "pdf-text-cache")


def _cache_setting():
    """(directory, beside) from PDF_TEXT_ROUTER_CACHE. The word `beside`
    puts every cache file next to its PDF; the directory is then only the
    fallback for folders that cannot be written to."""
    raw = (os.environ.get("PDF_TEXT_ROUTER_CACHE") or "").strip()
    if raw.lower() == "beside":
        return DEFAULT_CACHE_DIR, True
    return os.path.normpath(raw or DEFAULT_CACHE_DIR), False


CACHE_DIR, CACHE_BESIDE = _cache_setting()

MIN_TOTAL_CHARS = 50       # whole document below this -> treat as a scan
BLANK_PAGE_CHARS = 15      # a page below this has no usable text layer
# Page classes from the image share of the page (see page_kind). Values were
# read off the benchmark corpus: zenodo 22070313 p9 has 1 char and 70% image
# (text burned into a picture), p8 has 86 chars and 45% image (a figure with
# a caption); ndc population-projection p38/p102 have 0 chars and 0 image
# (truly blank); a vector map with 41 chars and 2% image is not caught, and
# that is accepted rather than guessing from path-object counts.
FIGURE_PAGE_CHARS = 200        # below this many chars a page can be a figure...
FIGURE_PAGE_IMAGE_AREA = 0.40  # ...if images cover at least this share of it
IMAGE_PAGE_MIN_AREA = 0.05     # a charless page with less image than this is blank
VISUAL_PAGE_LIMIT = 3      # an explicit range this short means "I want to look"
NATIVE_WHOLE_FILE_LIMIT = 10   # Claude Code reads <= this many pages poppler-free
MAX_RENDER_PAGES = 20      # Read's own per-call page limit; never hand back more
LARGE_TEXT_TOKENS = 40000  # above this, show a prefix and ask for Grep/offset
MAX_READ_LINES = 1800      # Read truncates around 2000 lines; stay under it
PREVIEW_LINES = 200        # rewrite mode: limit for a large text file
CACHE_MAX_AGE_DAYS = 30
CACHE_MAX_MB_DEFAULT = 500
TMP_MAX_AGE_SECONDS = 3600
PAGE_LIST_LIMIT = 20       # list at most this many page numbers per class


def _scale_from_env():
    try:
        v = float(os.environ.get("PDF_TEXT_ROUTER_SCALE", "") or 1.5)
    except ValueError:
        v = 1.5
    return min(max(v, 1.0), 3.0)


RENDER_SCALE = _scale_from_env()   # 1.5 ~ 108 DPI; legible without doubling tokens


def mode():
    """rewrite (default) or deny; anything else is read as rewrite."""
    v = (os.environ.get("PDF_TEXT_ROUTER_MODE") or "rewrite").strip().lower()
    return "deny" if v == "deny" else "rewrite"


def cache_max_bytes():
    try:
        mb = float(os.environ.get("PDF_TEXT_ROUTER_CACHE_MAX_MB", "")
                   or CACHE_MAX_MB_DEFAULT)
    except ValueError:
        mb = CACHE_MAX_MB_DEFAULT
    return int(mb * 1024 * 1024)


# --- hook I/O ---------------------------------------------------------------


def warn(msg):
    """One line on stderr. Claude Code keeps it in the debug log only."""
    try:
        sys.stderr.write("pdf-text-router: %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def emit(out):
    # ensure_ascii keeps the payload valid under any console encoding (cp950...)
    sys.stdout.write(json.dumps(out, ensure_ascii=True))
    sys.exit(0)


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
    emit(out)


def rewrite(updated_input, context, sysmsg=None):
    """allow + updatedInput: the same Read call now reads the new path."""
    out = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": updated_input,
        "additionalContext": context,
    }}
    if sysmsg:
        out["systemMessage"] = sysmsg
    emit(out)


# --- pdf engines ------------------------------------------------------------
# Preference order is deliberate:
#   pypdfium2  BSD-3/Apache-2.0, prebuilt wheels everywhere, extracts and renders
#   pymupdf    best-in-class but AGPL-3.0, so only used if already installed
#   pypdf      pure-python fallback; extracts only, and drops glyphs on complex
#              layouts (measured: -7.7% CJK characters on a two-column report)


class Engine(object):
    name = None
    can_render = False
    encrypted = False

    def __init__(self, path):
        self.path = path

    def page_images(self, i):
        """(share of the page covered by image objects, image count) for
        page i, or (None, None) when the engine cannot tell."""
        return None, None

    def close(self):
        pass


def _image_share(boxes, w, h):
    """Sum of box areas over the page area, clamped to 1.0: a full-bleed
    scan plus a logo must not read as 105%."""
    page = float(w) * float(h)
    if page <= 0:
        return None
    total = 0.0
    for l, b, r, t in boxes:
        total += max(r - l, 0.0) * max(t - b, 0.0)
    return min(total / page, 1.0)


class Pdfium(Engine):
    name = "pypdfium2"
    can_render = True

    def __init__(self, path):
        import pypdfium2
        self._m = pypdfium2
        self.doc = None
        Engine.__init__(self, path)
        try:
            self.doc = pypdfium2.PdfDocument(path)
        except pypdfium2.PdfiumError:
            # pdfium refuses to load a user-password file; every other load
            # failure is a real parse error and goes to the next engine.
            if pypdfium2.raw.FPDF_GetLastError() == pypdfium2.raw.FPDF_ERR_PASSWORD:
                self.encrypted = True
            else:
                raise

    def page_count(self):
        return len(self.doc)

    def texts(self):
        out = []
        for page in self.doc:
            out.append(page.get_textpage().get_text_bounded())
        return out

    def page_size_pt(self, i):
        return self.doc[i].get_size()

    def page_images(self, i):
        # max_depth=2 looks inside one level of form XObjects, where most
        # producers wrap their images; get_bounds() is (left, bottom,
        # right, top) in page points. ~17 ms per page measured.
        page = self.doc[i]
        image = self._m.raw.FPDF_PAGEOBJ_IMAGE
        boxes = [o.get_bounds() for o in page.get_objects(max_depth=2)
                 if o.type == image]
        w, h = page.get_size()
        return _image_share(boxes, w, h), len(boxes)

    def render(self, index, scale, dest):
        self.doc[index].render(scale=scale).to_pil().save(dest)
        return dest

    def close(self):
        # Without this, pdfium keeps the file open and Windows refuses to
        # delete or overwrite the PDF for the rest of the process.
        if self.doc is not None:
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
        # needs_pass, not is_encrypted: an owner-password-only file opens
        # with the empty user password and extracts normally in every engine.
        return bool(self.doc.needs_pass)

    def page_count(self):
        return self.doc.page_count

    def texts(self):
        return [p.get_text() for p in self.doc]

    def page_size_pt(self, i):
        r = self.doc[i].rect
        return (r.width, r.height)

    def page_images(self, i):
        page = self.doc[i]
        # bbox is (x0, y0, x1, y1) with y down; only the extents matter here.
        boxes = [(x0, y0, x1, y1) for x0, y0, x1, y1 in
                 (info["bbox"] for info in page.get_image_info())]
        r = page.rect
        return _image_share(boxes, r.width, r.height), len(boxes)

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
        r = self.reader
        return bool(r.is_encrypted and not r.decrypt(""))

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


def active_engine_name():
    """Name of the engine open_pdf() would try first, or None."""
    for cls in ENGINES:
        try:
            __import__(MODULE_OF[cls.name])
            return cls.name
        except ImportError:
            continue
    return None


def open_pdf(path):
    """First engine that imports and opens the file wins. None if all fail.

    A parse failure in one engine is not a verdict on the file: pdfium and
    MuPDF disagree on plenty of damaged PDFs, so each failure is logged and
    the next engine gets its turn.
    """
    for cls in ENGINES:
        try:
            return cls(path)
        except ImportError:
            continue
        except Exception as exc:
            warn("%s failed on %s: %s: %s" % (
                cls.name, os.path.basename(path), type(exc).__name__, exc))
            continue
    return None


# --- estimation -------------------------------------------------------------

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af"
                    r"\uf900-\ufaff\uff00-\uff60]+")


WS_RE = re.compile(r"\s+")


def is_cjk(ch):
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF
            or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFF60)


def est_text_tokens(text):
    """Latin runs ~4 chars/token; CJK runs closer to 1.3. Using one ratio for
    both is why an earlier version undercounted Chinese by 2-3x."""
    text = WS_RE.sub("", text)    # whitespace is free; a blank scan is 0 tokens
    cjk = len(text) - len(CJK_RE.sub("", text))
    return int(cjk / 1.3 + (len(text) - cjk) / 4.0)


IMAGE_LONG_EDGE_CAP = 2576   # the API downscales anything longer than this
IMAGE_TOKEN_CAP = 4784       # and never bills a single image above this


def est_image_tokens(w_pt, h_pt, pages, dpi=None, cap=IMAGE_LONG_EDGE_CAP,
                     token_cap=IMAGE_TOKEN_CAP):
    """Vision billing: ceil(w/28) * ceil(h/28) tokens per image, after the
    long edge is clamped to `cap` pixels, and never more than `token_cap`."""
    w, h = abs(w_pt), abs(h_pt)
    if dpi:
        w, h = w / 72.0 * dpi, h / 72.0 * dpi
    longest = max(w, h)
    if cap and longest > cap:
        k = cap / float(longest)
        w, h = w * k, h * k
    per_page = int(math.ceil(w / 28.0) * math.ceil(h / 28.0))
    if token_cap:
        per_page = min(per_page, token_cap)
    return per_page * max(pages, 0)


def has_poppler():
    return bool(shutil.which("pdftoppm"))


# --- helpers ----------------------------------------------------------------

BADPAGES = object()   # sentinel: `pages` was given but cannot be parsed
MAX_EXPAND = 100000   # longest range span_of will materialise
_PAGE_ITEM = re.compile(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?")


def span_of(pages):
    """Parse Read's `pages` argument into a sorted list of page numbers.

    "3" -> [3]; "1-5" -> [1..5]; "1,3,5" -> [1, 3, 5]; "5-3" -> [3, 4, 5].
    None or an empty string -> None (no range asked). Anything else that
    does not parse -> BADPAGES, so the caller can say why instead of letting
    Read fail down a path that may need poppler.
    """
    if pages is None:
        return None
    if isinstance(pages, bool):
        return BADPAGES
    if isinstance(pages, int):
        pages = str(pages)
    if not isinstance(pages, str):
        return BADPAGES
    if not pages.strip():
        return None
    out = set()
    for item in pages.split(","):
        m = _PAGE_ITEM.fullmatch(item)
        if not m:
            return BADPAGES
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a > b:
            a, b = b, a
        # "1-999999" is out of range, not unparseable; expand only as much
        # as any real document could need to say so.
        out.update(range(a, min(b, a + MAX_EXPAND) + 1))
    return sorted(out)


def describe_pages(pages):
    """[2, 3, 4, 7] -> '2-4,7' for messages."""
    runs, start, prev = [], None, None
    for p in pages:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            runs.append((start, prev))
            start = prev = p
    if start is not None:
        runs.append((start, prev))
    return ",".join("%d" % a if a == b else "%d-%d" % (a, b) for a, b in runs)


class _Strip(dict):
    """translate() table that drops every Unicode C* character except \\n and
    \\t, learning each character's category the first time it is seen. The
    per-character work is one C-level dict lookup after that."""

    def __missing__(self, code):
        ch = chr(code)
        keep = ch in "\n\t" or not unicodedata.category(ch).startswith("C")
        self[code] = code if keep else None
        return self[code]


_STRIP = _Strip()


def clean(text):
    """Normalise line endings and strip control characters that survive
    extraction. This includes the private-use area (category Co): a text
    layer made entirely of U+F0xx symbol-font glyphs is a scan in disguise,
    and dropping those is what makes such a page count as blank."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(_STRIP)


SAMPLE_BYTES = 262144   # hash the ends of very large files rather than all of it


def cache_key(path):
    """Keyed on size, mtime and content samples.

    size + mtime alone looks sufficient until you regenerate a PDF: exporting
    the same document twice often lands in the same second at the same byte
    count, and a stat-only key then serves the previous version's text.
    Content samples from both ends catch that; mtime catches an in-place
    edit that happens to leave both ends alone.
    """
    st = os.stat(path)
    h = hashlib.sha1()
    h.update(("%d|%d|" % (st.st_size, st.st_mtime_ns)).encode("ascii"))
    with open(path, "rb") as fh:
        h.update(fh.read(SAMPLE_BYTES))
        if st.st_size > SAMPLE_BYTES * 2:
            fh.seek(-SAMPLE_BYTES, os.SEEK_END)
        elif st.st_size > SAMPLE_BYTES:
            fh.seek(SAMPLE_BYTES)
        h.update(fh.read(SAMPLE_BYTES))
    return h.hexdigest()[:12]


def safe_name(path, limit=60):
    return re.sub(r"[^\w\-.]", "_", os.path.basename(path))[:limit]


def dir_writable(d):
    return os.path.isdir(d) and os.access(d, os.W_OK)


_WARNED_DIRS = set()


def cache_dir_for(src, central=False):
    """(directory, beside) for the cache files of `src`.

    In beside mode that is the PDF's own folder unless it cannot be written
    to, in which case the central directory is used and one line goes to
    stderr the first time a folder falls back. `central` forces the central
    directory (used after a write beside the PDF raised PermissionError:
    os.access says yes to plenty of folders Windows then refuses)."""
    if central or not CACHE_BESIDE:
        return CACHE_DIR, False
    d = os.path.dirname(os.path.abspath(src))
    if dir_writable(d):
        return d, True
    if d not in _WARNED_DIRS:
        _WARNED_DIRS.add(d)
        warn("beside: %s is not writable, caching in %s instead" % (d, CACHE_DIR))
    return CACHE_DIR, False


def in_central(dest):
    return (os.path.normcase(os.path.abspath(os.path.dirname(dest)))
            == os.path.normcase(os.path.abspath(CACHE_DIR)))


def text_cache_path(path, key=None, central=False):
    d, beside = cache_dir_for(path, central)
    if beside:
        # No key in the name: the sidecar carries it and is checked on a hit.
        return os.path.join(d, os.path.basename(path) + ".txt")
    return os.path.join(d, "%s-%s.txt" % (key or cache_key(path), safe_name(path)))


def sidecar_path(text_path):
    return text_path[:-4] + ".json"


def image_cache_path(path, page, scale, key=None, central=False):
    d, beside = cache_dir_for(path, central)
    if beside:
        # Scale in the name, as in the central dir, so a new
        # PDF_TEXT_ROUTER_SCALE does not serve the old rendering.
        return os.path.join(d, "%s.p%d.%.1fx.png"
                            % (os.path.basename(path), page, scale))
    return os.path.join(d, "%s-p%d-%.1fx-%s.png" % (
        key or cache_key(path), page, scale, safe_name(path, 40)))


TEXT_HEADER = "# Text extracted from:"
SIDECAR_KEYS = ("pages", "chars", "per_page_chars", "page_raw_chars",
                "page_image_area", "w_pt", "h_pt", "engine", "text_tokens",
                "lines", "src", "key")


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


def touch(path):
    """A hit refreshes mtime, so pruning by mtime means least-recently-used
    rather than oldest-written."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def load_sidecar(text_path, key=None):
    """The sidecar plus a complete .txt is enough to decide without opening
    the PDF at all. None if either is missing, inconsistent, written for a
    different key (beside mode has no key in the file name), lacking a field
    this version needs (an older sidecar has no page_image_area, so it is
    re-extracted rather than misclassified), or extracted by a different
    engine than the one that would run now (engines disagree on glyphs, so
    a cache from pypdf must not outlive an install of pypdfium2)."""
    side = sidecar_path(text_path)
    if not (os.path.isfile(side) and os.path.isfile(text_path)
            and text_cache_is_complete(text_path)):
        return None
    try:
        with open(side, encoding="utf-8") as fh:
            meta = json.load(fh)
        if not all(k in meta for k in SIDECAR_KEYS):
            return None
        n = meta["pages"]
        if n != len(meta["per_page_chars"]) or n < 1:
            return None
        if n != len(meta["page_image_area"]) or n != len(meta["page_raw_chars"]):
            return None
        if key is not None and meta["key"] != key:
            return None
        if meta["engine"] != active_engine_name():
            return None
        if in_central(text_path):
            touch(text_path)
            touch(side)
        return meta
    except (OSError, ValueError, TypeError):
        return None


def atomic_write(dest, data, binary=False):
    tmp = "%s.tmp%d" % (dest, os.getpid())
    if binary:
        with open(tmp, "wb") as fh:
            fh.write(data)
    else:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
    os.replace(tmp, dest)     # atomic: readers see the old file or the new one


def sweep_temp_files(now=None):
    """A hook killed mid-write leaves a .tmp<pid> behind; clear old ones."""
    now = now or time.time()
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return
    for name in names:
        if ".tmp" not in name:
            continue
        p = os.path.join(CACHE_DIR, name)
        try:
            if now - os.path.getmtime(p) > TMP_MAX_AGE_SECONDS:
                os.remove(p)
        except OSError:
            pass


def prune_cache(max_bytes=None, max_age_days=CACHE_MAX_AGE_DAYS, now=None,
                keep=()):
    """Drop entries older than max_age_days, then the oldest until the
    directory is under max_bytes. Returns the number of files removed."""
    now = now or time.time()
    max_bytes = cache_max_bytes() if max_bytes is None else max_bytes
    keep = set(os.path.normcase(os.path.abspath(k)) for k in keep)
    entries = []
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return 0
    for name in names:
        p = os.path.join(CACHE_DIR, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        entries.append((st.st_mtime, st.st_size, p))
    entries.sort()
    total = sum(e[1] for e in entries)
    removed = 0
    cutoff = now - max_age_days * 86400
    for mtime, size, p in entries:
        if os.path.normcase(os.path.abspath(p)) in keep:
            continue
        if mtime >= cutoff and total <= max_bytes:
            break
        try:
            os.remove(p)
            removed += 1
            total -= size
        except OSError:
            pass
    return removed


def page_tag(kind, chars, area, raw_chars=None):
    """The suffix on a page marker in the .txt; empty for a text page."""
    if kind == "blank":
        return " (blank page)"
    if kind == "image":
        if unreadable(chars, raw_chars):
            return " (image page, text layer unreadable)"
        return " (image page, no text layer)"
    if kind == "figure":
        return " (figure page: %d%% image, %d chars)" % (round(area * 100), chars)
    return ""


def write_text_cache(dest, src, pages_text, kinds=None, areas=None, meta=None,
                     raw_chars=None):
    """Write the .txt (and, if meta is given, its .json sidecar). Returns the
    line count, which is what the messages quote. kinds/areas/raw_chars are
    per page (see classify); without them every page is marked as text."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    central = in_central(dest)
    if central:
        sweep_temp_files()
    n = len(pages_text)
    kinds = kinds or ["text"] * n
    areas = areas or [None] * n
    raw_chars = raw_chars or [None] * n
    total = sum(len(t) for t in pages_text)
    parts = ["%s %s\n# %d pages, %d chars (text layer, no OCR needed)\n"
             % (TEXT_HEADER, src, n, total)]
    page_lines, line = [], 1
    for i, t in enumerate(pages_text, 1):
        line += parts[-1].count("\n")
        page_lines.append(line + 2)     # the marker follows two newlines
        tag = page_tag(kinds[i - 1], len(t.strip()), areas[i - 1] or 0.0,
                       raw_chars[i - 1])
        parts.append("\n\n===== [page %d/%d]%s =====\n\n%s"
                     % (i, n, tag, t.rstrip()))
    atomic_write(dest, "".join(parts))
    lines = count_lines(dest)
    if meta is not None:
        meta.update(lines=lines, page_lines=page_lines, src=src)
        atomic_write(sidecar_path(dest), json.dumps(meta))
    if central:
        # Never prune beside the PDF: that is the user's folder, not ours.
        prune_cache(keep=(dest, sidecar_path(dest)))
    return lines


def render_pages(engine, path, pages, scale=RENDER_SCALE, key=None,
                 central=False):
    """Render the given page numbers here so the caller never needs poppler."""
    key = key or cache_key(path)
    out = []
    n = engine.page_count()
    first = True
    for page in pages:
        if page < 1 or page > n:
            continue
        dest = image_cache_path(path, page, scale, key, central=central)
        in_cache = in_central(dest)
        if first:
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            if in_cache:
                sweep_temp_files()
            first = False
        fresh = os.path.isfile(dest) and png_is_complete(dest)
        if fresh and not in_cache:
            # Beside the PDF the name carries no key, so a PNG older than
            # the PDF is from a previous version of it.
            fresh = os.path.getmtime(dest) >= os.path.getmtime(path)
        if fresh:
            if in_cache:
                touch(dest)
        else:
            tmp = "%s.tmp%d.png" % (dest[:-4], os.getpid())
            engine.render(page - 1, scale, tmp)
            os.replace(tmp, dest)
        out.append((page, dest))
    if out and in_central(out[0][1]):
        prune_cache(keep=[d for _, d in out])
    return out


# --- decision ---------------------------------------------------------------

class TextResult(collections.namedtuple("TextResult", (
        "n total text_tokens img_tokens path_note native_works engine dest lines "
        "kinds requested page_lines src file_tokens"))):
    """kinds is the per-page class list from classify(); the three
    properties below are the 1-based page numbers of each non-text class."""
    __slots__ = ()

    @property
    def blank_pages(self):
        return pages_of(self.kinds, "blank")

    @property
    def image_pages(self):
        return pages_of(self.kinds, "image")

    @property
    def figure_pages(self):
        return pages_of(self.kinds, "figure")


RenderResult = collections.namedtuple("RenderResult", (
    "first count n scanned imgs pages rest src"))


def inspect_pdf(engine):
    """Everything the decision needs, in the same shape as the sidecar."""
    raw_text = engine.texts()
    pages_text = [clean(t) for t in raw_text]
    w_pt, h_pt = engine.page_size_pt(0)
    per_page = [len(t.strip()) for t in pages_text]
    # Counted before clean(): a page that had characters and lost them all
    # is a symbol-font text layer, not an empty page (see page_kind).
    raw_chars = [len(t.strip()) for t in raw_text]
    areas, counts, failed = [], [], 0
    for i in range(len(pages_text)):
        try:
            area, count = engine.page_images(i)
        except Exception:
            # One page's broken image dictionary must not sink the document;
            # None means "unknown" and page_kind falls back to chars alone.
            area, count = None, None
            failed += 1
        areas.append(area)
        counts.append(count)
    if failed:
        warn("%s: image scan failed on %d page(s), classified by chars only"
             % (os.path.basename(engine.path), failed))
    meta = {
        "pages": len(pages_text),
        "chars": sum(per_page),
        "per_page_chars": per_page,
        "page_raw_chars": raw_chars,
        "page_image_area": areas,
        "page_image_count": counts,
        "w_pt": float(w_pt),
        "h_pt": float(h_pt),
        "engine": engine.name,
        "text_tokens": est_text_tokens("".join(pages_text)),
    }
    return meta, pages_text


def unreadable(chars, raw_chars):
    """A page that had a text layer before clean() stripped it: private-use
    glyphs from a symbol font. Nothing to read, but not an empty page."""
    return (chars < BLANK_PAGE_CHARS and raw_chars is not None
            and raw_chars >= BLANK_PAGE_CHARS)


def page_kind(chars, area, raw_chars=None):
    """blank | image | figure | text for one page (see the module docstring).
    A page is blank only when nothing was there at all: under
    BLANK_PAGE_CHARS both after and before clean(), and under
    IMAGE_PAGE_MIN_AREA of image. With no image measurement (pypdf, or a
    page that failed) a charless page is called an image page, the
    conservative choice: Claude goes and looks rather than skipping
    something that was there."""
    if chars < BLANK_PAGE_CHARS:
        if area is None or area >= IMAGE_PAGE_MIN_AREA:
            return "image"
        return "image" if unreadable(chars, raw_chars) else "blank"
    if (area is not None and chars < FIGURE_PAGE_CHARS
            and area >= FIGURE_PAGE_IMAGE_AREA):
        return "figure"
    return "text"


def pages_of(kinds, kind):
    """1-based page numbers of one class."""
    return [i + 1 for i, k in enumerate(kinds or ()) if k == kind]


def classify(meta):
    """(kinds, scanned): one class per page, and whether the document as a
    whole is a scan. The scan rule counts every page under BLANK_PAGE_CHARS
    as a non-text page whatever its image share; it was validated on all
    35 corpus files and is independent of the image measurement."""
    n = meta["pages"]
    per_page = meta["per_page_chars"]
    areas = meta.get("page_image_area") or [None] * n
    raw = meta.get("page_raw_chars") or [None] * n
    kinds = [page_kind(c, a, r) for c, a, r in zip(per_page, areas, raw)]
    no_text = sum(1 for c in per_page if c < BLANK_PAGE_CHARS)
    scanned = meta["chars"] < MIN_TOTAL_CHARS or (n - no_text) * 2 < n
    return kinds, scanned


def decide(file_path, pages_arg, dry_run=False):
    """Returns (action, payload). Side-effect free when dry_run is set.
    Never raises: anything unexpected is logged and becomes an allow."""
    try:
        return _decide(file_path, pages_arg, dry_run)
    except Exception as exc:
        warn("giving up on %s: %s: %s" % (
            os.path.basename(file_path), type(exc).__name__, exc))
        return ("allow", "unexpected error: %s" % type(exc).__name__)


def _beside_fallback(file_path, dest, exc):
    """A write beside the PDF failed after os.access said it would work.
    Returns True if the caller should retry in the central directory."""
    if in_central(dest) or not isinstance(exc, OSError):
        return False
    warn("beside: cannot write in %s (%s: %s), caching in %s instead"
         % (os.path.dirname(dest), type(exc).__name__, exc, CACHE_DIR))
    return True


def _decide(file_path, pages_arg, dry_run):
    key = cache_key(file_path)
    dest = text_cache_path(file_path, key)
    meta = load_sidecar(dest, key)
    if meta is None and not in_central(dest):
        # Beside mode, but an earlier run may have had to fall back.
        alt = text_cache_path(file_path, key, central=True)
        meta = load_sidecar(alt, key)
        if meta is not None:
            dest = alt
    cached = meta is not None
    engine = None
    pages_text = None
    try:
        if not cached:
            engine = open_pdf(file_path)
            if engine is None:
                return ("allow", "no usable pdf engine, or the file failed to parse")
            if engine.encrypted:
                return ("allow", "encrypted; let Read report that itself")
            if engine.page_count() == 0:
                return ("allow", "zero pages")
            meta, pages_text = inspect_pdf(engine)
            meta["key"] = key

        n, total = meta["pages"], meta["chars"]
        per_page = meta["per_page_chars"]
        kinds, scanned = classify(meta)

        requested = span_of(pages_arg)
        if requested is BADPAGES:
            return ("badpages", (pages_arg, n))
        if requested:
            bad = [p for p in requested if p < 1 or p > n]
            if bad:
                return ("outofrange", (bad[0], n))
        span = len(requested) if requested else None
        # A range that covers the whole document is a read, not a look.
        wants_layout = span is not None and span <= VISUAL_PAGE_LIMIT and span < n
        # Claude Code only shells out to pdftoppm when a page range is involved;
        # a short whole-file read goes straight to the API as a document block.
        native_needs_poppler = (span is not None) or (n > NATIVE_WHOLE_FILE_LIMIT)
        native_works = has_poppler() or not native_needs_poppler

        if scanned or wants_layout:
            why = "scan" if scanned else "explicit short range"
            if native_works:
                return ("allow", "%s; native render path is available" % why)
            if engine is None:
                engine = open_pdf(file_path)
                if engine is None:
                    return ("allow", "%s; no engine could reopen the file" % why)
            if not engine.can_render:
                return ("allow", "%s; %s cannot render" % (why, engine.name))
            if requested:
                to_render = requested[:MAX_RENDER_PAGES]
                rest = len(requested) - len(to_render)
            else:
                # Whole-file read of a long scan: same budget Read itself
                # applies to a whole file, the rest on request.
                to_render = list(range(1, min(n, NATIVE_WHOLE_FILE_LIMIT) + 1))
                rest = n - len(to_render)
            res = RenderResult(to_render[0], len(to_render), n, scanned, None,
                               to_render, rest, file_path)
            if dry_run:
                return ("render", res)
            try:
                imgs = render_pages(engine, file_path, to_render, key=key)
            except Exception as exc:
                probe = image_cache_path(file_path, to_render[0], RENDER_SCALE, key)
                if not _beside_fallback(file_path, probe, exc):
                    raise
                imgs = render_pages(engine, file_path, to_render, key=key,
                                    central=True)
            if not imgs:
                return ("outofrange", (to_render[0], n))
            return ("render", res._replace(imgs=imgs))

        w_pt, h_pt = meta["w_pt"], meta["h_pt"]
        text_tokens = meta["text_tokens"]
        if requested:
            # Bill both sides on the same pages: the text share of those pages
            # against images of exactly those pages.
            share = (sum(per_page[p - 1] for p in requested) / float(total)
                     if total else 1.0)
            text_tokens = int(text_tokens * share)
            billed = len(requested)
        else:
            billed = n
        if native_needs_poppler:
            # poppler path: pdftoppm -jpeg -r 100, images only, no text layer.
            img_tokens = est_image_tokens(w_pt, h_pt, billed, dpi=100)
            path_note = "poppler path (100 DPI JPEG, no text layer attached)"
        else:
            # API document block: every page as an image *and* its text
            img_tokens = est_image_tokens(w_pt, h_pt, n) + text_tokens
            path_note = "API document block (an image plus the text of every page)"

        res = TextResult(n, total, text_tokens, img_tokens, path_note,
                         native_works, meta["engine"], None, None, kinds,
                         requested, None, file_path, meta["text_tokens"])
        if dry_run:
            return ("text", res)
        if cached:
            lines, page_lines = meta["lines"], meta.get("page_lines")
        else:
            areas, raw = meta["page_image_area"], meta["page_raw_chars"]
            try:
                lines = write_text_cache(dest, file_path, pages_text, kinds,
                                         areas, meta=meta, raw_chars=raw)
            except Exception as exc:
                if not _beside_fallback(file_path, dest, exc):
                    raise
                dest = text_cache_path(file_path, key, central=True)
                lines = write_text_cache(dest, file_path, pages_text, kinds,
                                         areas, meta=meta, raw_chars=raw)
            page_lines = meta["page_lines"]     # filled in by write_text_cache
        return ("text", res._replace(dest=dest, lines=lines,
                                     page_lines=page_lines))
    finally:
        if engine is not None:
            engine.close()


# --- messages ---------------------------------------------------------------


def _page_list(pages):
    """[1, 2, ..., 30] -> 'Pages 1, 2, ..., 20 and 10 more' with the grammar
    a caller needs: (label, listed, is/are, has/have, it/them)."""
    shown = pages[:PAGE_LIST_LIMIT]
    listed = ", ".join(str(p) for p in shown)
    more = len(pages) - len(shown)
    if more:
        listed += " and %d more" % more
    one = len(pages) == 1
    return ("Page" if one else "Pages", listed, "is" if one else "are",
            "has" if one else "have", "it" if one else "them")


def page_kinds_note(kinds, n):
    """One sentence per non-text page class present, as a list of strings.
    Blank pages are something to skip; image and figure pages are something
    to look at, and the figure sentence says why the text alone may
    mislead."""
    out = []
    blank = pages_of(kinds, "blank")
    if blank:
        word, listed, are, _, them = _page_list(blank)
        out.append("%s %s of %d %s blank (no text, no image); skip %s."
                   % (word, listed, n, are, them))
    image = pages_of(kinds, "image")
    if image:
        word, listed, _, have, _ = _page_list(image)
        out.append("%s %s of %d %s no readable text layer (marked \"(image "
                   "page, ...)\" in the extraction; likely pictures, scans or "
                   "symbol fonts). Read the .pdf with pages=\"%d\" to see one."
                   % (word, listed, n, have, image[0]))
    figure = pages_of(kinds, "figure")
    if figure:
        word, listed, are, _, them = _page_list(figure)
        out.append("%s %s of %d %s mostly picture with little text (marked "
                   "\"(figure page: ...)\" in the extraction) -- the text layer "
                   "may miss what the figure says; Read the .pdf with "
                   "pages=\"%d\" to see %s."
                   % (word, listed, n, are, figure[0],
                      "it" if len(figure) == 1 else "one of " + them))
    return out


def render_message(first, count, n, scanned, imgs, rest=0):
    listing = "\n".join("  page %d -> %s" % (p, f) for p, f in imgs)
    if scanned:
        why = ("This PDF has no text layer, so it has to be looked at rather than "
               "parsed")
    else:
        why = ("You asked for a short page range, which means you want to see the "
               "layout rather than read the words")
    tail = ("For other pages, ask for another range (pages=\"N\" or \"N-M\", up to "
            "%d pages per call) and this hook will render it the same way."
            % MAX_RENDER_PAGES)
    if rest:
        tail = ("%d more page(s) were not rendered in this call. " % rest) + tail
    return (
        "{why} -- but Claude Code's page-render path shells out to poppler's "
        "`pdftoppm`, which is not on PATH here, so that Read would have failed.\n\n"
        "{k} of {n} page(s) have been rendered locally instead:\n{listing}\n\n"
        "ACTION: call Read on those .png paths. Your vision is the OCR engine; the "
        "images are all you need.\n\n{tail}"
        .format(why=why, k=len(imgs), n=n, listing=listing, tail=tail))


def render_context(res):
    """additionalContext for rewrite mode: this Read already shows page one."""
    imgs = res.imgs
    if res.scanned:
        why = "This PDF has no text layer"
    else:
        why = "You asked for a short page range (a look at the layout)"
    lines = ["%s, and Claude Code's page-render path needs poppler's `pdftoppm`, "
             "which is not on PATH here. The hook rendered %d of %d page(s) "
             "locally at %.1fx and pointed this Read at the first one:"
             % (why, len(imgs), res.n, RENDER_SCALE)]
    lines += ["  page %d -> %s%s" % (p, f, "  (this Read)" if i == 0 else "")
              for i, (p, f) in enumerate(imgs)]
    if len(imgs) > 1:
        lines.append("Read the other .png paths to see those pages.")
    if res.rest:
        lines.append("%d more page(s) were not rendered in this call." % res.rest)
    lines.append("For other pages, Read the .pdf with pages=\"N\" or \"N-M\" "
                 "(up to %d pages per call) and the hook will render them the "
                 "same way." % MAX_RENDER_PAGES)
    return "\n".join(lines)


def text_message(n, total, text_tokens, img_tokens, path_note, native_works,
                 dest, lines, kinds=None, requested=None):
    saved = img_tokens - text_tokens
    what = ("pages %s" % describe_pages(requested)) if requested else "it"
    if native_works:
        if saved > 0:
            head = ("This PDF has a real text layer ({n} pages, {c:,} chars). Reading "
                    "{w} as pages goes through the {note}, an estimated ~{i:,} tokens "
                    "against ~{t:,} for the text itself -- roughly {s:,} spent on "
                    "pictures of words.".format(n=n, c=total, note=path_note, w=what,
                                                 i=img_tokens, t=text_tokens,
                                                 s=saved))
        else:
            head = ("This PDF has a real text layer ({n} pages, {c:,} chars). Text is "
                    "not cheaper here (~{t:,} vs ~{i:,} tokens at 100 DPI) but it is "
                    "exact, greppable and needs no poppler."
                    .format(n=n, c=total, t=text_tokens, i=img_tokens))
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

    if requested:
        body += ("\n\nYou asked for pages %s; find them by their [page N/%d] "
                 "markers." % (describe_pages(requested), n))

    for note in page_kinds_note(kinds, n):
        body += "\n\n" + note

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


def text_rewrite_input(tool_input, res):
    """updatedInput for the text path: the .txt, plus offset/limit when the
    caller did not set them and the file or the request calls for it.
    Returns (updated_input, window_note)."""
    upd = {"file_path": res.dest}
    if "offset" in tool_input or "limit" in tool_input:
        return upd, None
    large = res.file_tokens > LARGE_TEXT_TOKENS or res.lines > MAX_READ_LINES
    if res.requested and res.page_lines:
        first, last = res.requested[0], res.requested[-1]
        # One line before the marker: correct whether offset is 0- or 1-based.
        start = max(res.page_lines[first - 1] - 1, 1)
        end = (res.page_lines[last] - 1 if last < res.n else res.lines + 1)
        limit = max(end - start, 1)
        upd["offset"] = start
        upd["limit"] = min(limit, MAX_READ_LINES)
        note = ("positioned at line %d (page %d)" % (start, first))
        if limit > MAX_READ_LINES:
            note += (", showing the first %d of the %d lines those pages span"
                     % (MAX_READ_LINES, limit))
        else:
            note += ", %d lines (pages %s)" % (limit, describe_pages(res.requested))
        return upd, note
    if large:
        upd["limit"] = PREVIEW_LINES
        return upd, "showing the first %d lines" % PREVIEW_LINES
    return upd, None


def text_context(res, window_note=None):
    """additionalContext for rewrite mode."""
    saved = res.img_tokens - res.text_tokens
    lines = ["This Read was redirected from the PDF to its extracted text layer "
             "(this is the .txt, not the .pdf)."]
    lines.append("  source: %s (%d pages, %s chars, via %s)"
                 % (res.src, res.n, format(res.total, ","), res.engine))
    lines.append("  text:   %s (%s lines, ~%s tokens)"
                 % (res.dest, format(res.lines, ","),
                    format(res.file_tokens, ",")))
    what = ("Pages %s" % describe_pages(res.requested) if res.requested
            else "The document")
    if saved > 0:
        lines.append("%s as text: ~%s tokens; as page images via the %s: ~%s."
                     % (what, format(res.text_tokens, ","), res.path_note,
                        format(res.img_tokens, ",")))
    else:
        lines.append("%s as text: ~%s tokens vs ~%s as page images at 100 DPI -- "
                     "text is not cheaper here but it is exact, greppable and "
                     "needs no poppler."
                     % (what, format(res.text_tokens, ","),
                        format(res.img_tokens, ",")))
    lines.append("Every page starts with a \"===== [page N/%d] =====\" marker; "
                 "cite page numbers from those." % res.n)
    large = res.file_tokens > LARGE_TEXT_TOKENS or res.lines > MAX_READ_LINES
    if window_note:
        lines.append("The file has %s lines / ~%s tokens; this call is %s. "
                     "Grep the .txt for what you need, or Read it again with "
                     "offset/limit for other parts."
                     % (format(res.lines, ","), format(res.file_tokens, ","),
                        window_note))
    elif large:
        lines.append("The file has %s lines / ~%s tokens, more than Read shows in "
                     "one call (it stops around 2000 lines). Grep the .txt for what "
                     "you need, or Read it with offset/limit."
                     % (format(res.lines, ","), format(res.file_tokens, ",")))
    lines.extend(page_kinds_note(res.kinds, res.n))
    lines.append("To look at a figure, a table or the layout, Read the .pdf again "
                 "with a range of %d pages or fewer (e.g. pages=\"4-5\")."
                 % VISUAL_PAGE_LIMIT)
    return "\n".join(lines)


def badpages_message(pages_arg, n):
    return ("pages must be N, N-M or a comma list like 1,3,5 (got %r); this "
            "document has %d pages." % (str(pages_arg), n))


def outofrange_message(bad, n):
    return ("This PDF has {n} page(s), so page {b} does not exist. Normally Read "
            "would tell you that, but on a machine without poppler you would "
            "have got an unrelated error about `pdftoppm` instead.\n\n"
            "ACTION: re-run Read with a range inside 1-{n}, or with no `pages` "
            "argument at all.".format(n=n, b=bad))


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        allow()
    if not isinstance(data, dict) or data.get("tool_name") != "Read":
        allow()
    ti = data.get("tool_input") or {}
    if not isinstance(ti, dict):
        allow()
    fp = ti.get("file_path") or ""
    if not isinstance(fp, str) or not fp.lower().endswith(".pdf") \
            or not os.path.isfile(fp):
        allow()

    action, payload = decide(fp, ti.get("pages"))

    if action == "allow":
        allow()

    if action == "badpages":
        pages_arg, n = payload
        deny(badpages_message(pages_arg, n),
             sysmsg="Unparseable pages=%r" % (str(pages_arg),))

    if action == "outofrange":
        bad, n = payload
        deny(outofrange_message(bad, n),
             sysmsg="Requested page %d is outside 1-%d" % (bad, n))

    use_rewrite = mode() == "rewrite"

    if action == "render":
        res = payload
        sysmsg = "No poppler: rendered %d page(s) locally" % len(res.imgs)
        if use_rewrite:
            rewrite({"file_path": res.imgs[0][1]}, render_context(res), sysmsg)
        deny(render_message(res.first, res.count, res.n, res.scanned, res.imgs,
                            res.rest), sysmsg=sysmsg)

    res = payload
    sysmsg = ("PDF text layer via %s -- reading text, ~%s tokens instead of ~%s"
              % (res.engine, format(res.text_tokens, ","),
                 format(res.img_tokens, ",")))
    if use_rewrite:
        upd, window_note = text_rewrite_input(ti, res)
        rewrite(upd, text_context(res, window_note), sysmsg)
    deny(text_message(res.n, res.total, res.text_tokens, res.img_tokens,
                      res.path_note, res.native_works, res.dest, res.lines,
                      res.kinds, res.requested), sysmsg=sysmsg)


# --- cli --------------------------------------------------------------------


def cache_usage():
    """(files, bytes) in CACHE_DIR."""
    files = size = 0
    try:
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            if os.path.isfile(p):
                files += 1
                size += os.path.getsize(p)
    except OSError:
        pass
    return files, size


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
                print("  rendering        ready (scale %.1fx)" % RENDER_SCALE)
            except ImportError:
                print("  rendering        UNAVAILABLE: pypdfium2 needs Pillow to "
                      "write PNGs (`pip install pillow`)")
        else:
            print("  rendering        ready (scale %.1fx)" % RENDER_SCALE)
    else:
        print("  rendering        unavailable with %s (extract only)" % active.name)

    for tool in ("pdftoppm", "pdfinfo"):
        where = shutil.which(tool)
        print("  poppler %-8s %s" % (tool, where if where else "not on PATH"))
    if not shutil.which("pdftoppm"):
        print("                   -> PDFs over %d pages, and any read with a page "
              "range, cannot be rendered natively; this hook is their only "
              "working path" % NATIVE_WHOLE_FILE_LIMIT)
    m = mode()
    print("  mode             %s (%s)" % (m, (
        "allow + updatedInput: the same Read returns the .txt/.png"
        if m == "rewrite" else
        "deny + ACTION message: Claude re-reads the new path")))
    if CACHE_BESIDE:
        print("  cache mode       beside (files go next to each PDF as "
              "<name>.pdf.txt/.json/.p<N>.<scale>x.png; no pruning there)")
        print("  fallback dir     %s (for folders that cannot be written to)"
              % CACHE_DIR)
    else:
        print("  cache mode       central")
        print("  cache dir        %s" % CACHE_DIR)
    files, size = cache_usage()
    print("  cache usage      %d file(s), %.1f MB (limit %d MB, %d days)"
          % (files, size / 1048576.0, cache_max_bytes() // 1048576,
             CACHE_MAX_AGE_DAYS))
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
    m = mode()
    print("%s\n  decision       %s" % (path, action))
    if action == "allow":
        print("  reason         %s" % payload)
        print("  output         none (Read proceeds untouched)")
    elif action == "outofrange":
        print("  reason         requested page %d, file has %d" % payload)
        print("  output         deny")
    elif action == "badpages":
        print("  reason         cannot parse pages=%r" % (payload[0],))
        print("  output         deny")
    elif action == "render":
        res = payload
        print("  reason         %s, and poppler is missing"
              % ("no text layer" if res.scanned else "short explicit range"))
        print("  would render   pages %s of %d" % (describe_pages(res.pages), res.n))
        print("  output         %s" % (
            "rewrite (Read the first .png; others listed in additionalContext)"
            if m == "rewrite" else "deny (ACTION: Read the .png paths)"))
    else:
        res = payload
        print("  engine         %s" % res.engine)
        print("  pages / chars  %d / %d" % (res.n, res.total))
        for label, pages in (("blank pages", res.blank_pages),
                             ("image pages", res.image_pages),
                             ("figure pages", res.figure_pages)):
            if pages:
                print("  %-14s %s" % (label, describe_pages(pages)))
        print("  native path    %s (%s)" % ("works" if res.native_works else "BROKEN",
                                            res.path_note))
        print("  text tokens    ~%s" % format(res.text_tokens, ","))
        print("  render tokens  ~%s" % format(res.img_tokens, ","))
        print("  ratio          %s" % ("%.2fx" % (res.img_tokens / float(res.text_tokens))
                                       if res.text_tokens else "n/a"))
        print("  output         %s" % (
            "rewrite (Read reads the extracted .txt)" if m == "rewrite"
            else "deny (ACTION: Read the extracted .txt)"))
    return 0


def clear_cache():
    if CACHE_BESIDE:
        print("PDF_TEXT_ROUTER_CACHE=beside: cache files live next to their PDFs "
              "as <name>.pdf.txt, <name>.pdf.json and <name>.pdf.p<N>.<scale>x.png.\n"
              "Nothing was deleted; remove those files yourself, or unset the "
              "variable to clear the central directory (%s)." % CACHE_DIR)
        return 0
    files, size = cache_usage()
    removed = 0
    try:
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
    except OSError as exc:
        print("cache dir %s: %s" % (CACHE_DIR, exc))
        return 1
    print("cleared %s: %d file(s), %.1f MB" % (CACHE_DIR, removed,
                                               size / 1048576.0))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--clear-cache" in sys.argv:
        sys.exit(clear_cache())
    if "--check" in sys.argv:
        i = sys.argv.index("--check")
        sys.exit(check(sys.argv[i + 1] if len(sys.argv) > i + 1 else ""))
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        warn("hook crashed, allowing the Read: %s: %s" % (type(exc).__name__, exc))
        allow()   # fail-open: nothing here is worth blocking a Read over
