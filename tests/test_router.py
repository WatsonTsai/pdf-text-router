"""Tests for pdf-text-router.

Run: python -m pytest tests -q   (or: python -m unittest discover -s tests)

Some of these exist because an earlier version of this suite passed while the
thing it claimed to test was broken. Where that happened, the test says so.
"""

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import pdf_text_router as R          # noqa: E402
import fixtures                      # noqa: E402

HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks", "pdf_text_router.py")

CJK = "研究計畫：氮素利用效率與生物性硝化抑制。第一節、材料與方法。"


class TempCache(unittest.TestCase):
    """Every test writes into a throwaway cache dir, never the user's."""

    PATCHED = ("CACHE_DIR", "CACHE_BESIDE", "has_poppler", "ENGINES", "open_pdf",
               "dir_writable", "atomic_write", "prune_cache", "available_engines")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ptr-test-")
        self._saved = dict((k, getattr(R, k)) for k in self.PATCHED)
        R.CACHE_DIR = os.path.join(self.tmp, "cache")
        R.CACHE_BESIDE = False
        R._WARNED_DIRS.clear()
        self._pdfium_init = R.Pdfium.__init__
        self._stderr = sys.stderr
        sys.stderr = io.StringIO()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(R, k, v)
        R.Pdfium.__init__ = self._pdfium_init
        sys.stderr = self._stderr
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stderr(self):
        return sys.stderr.getvalue()

    def poppler(self, present):
        R.has_poppler = lambda: present

    def text_file(self, pages=2, name=None, **kw):
        return fixtures.write(self.tmp, name or "t%d.pdf" % pages,
                              fixtures.text_pdf(pages, **kw))

    def blank_file(self, pages=2, name=None):
        return fixtures.write(self.tmp, name or "b%d.pdf" % pages,
                              fixtures.blank_pdf(pages))

    def mixed_file(self, name="mixed.pdf"):
        """text, blank, image, figure -- one page of each."""
        return fixtures.write(self.tmp, name, fixtures.mixed_pdf())


def kinds_with(n, **classes):
    """kinds_with(110, blank=[38, 76]) -> a 110-entry kinds list."""
    kinds = ["text"] * n
    for kind, pages in classes.items():
        for p in pages:
            kinds[p - 1] = kind
    return kinds


# --- pure helpers -----------------------------------------------------------


class TestPages(unittest.TestCase):

    def test_single_and_ranges(self):
        self.assertEqual(R.span_of("3"), [3])
        self.assertEqual(R.span_of("1-5"), [1, 2, 3, 4, 5])
        self.assertEqual(R.span_of(" 2 - 4 "), [2, 3, 4])

    def test_comma_lists(self):
        self.assertEqual(R.span_of("1,3,5"), [1, 3, 5])
        self.assertEqual(R.span_of("1-3,7"), [1, 2, 3, 7])
        self.assertEqual(R.span_of(" 7 , 1-2 "), [1, 2, 7])
        self.assertEqual(R.span_of("2,2,1-2"), [1, 2])

    def test_reversed_range_is_read_forwards(self):
        self.assertEqual(R.span_of("5-3"), [3, 4, 5])

    def test_absent_means_no_range(self):
        self.assertIsNone(R.span_of(None))
        self.assertIsNone(R.span_of(""))
        self.assertIsNone(R.span_of("   "))

    def test_an_integer_is_tolerated(self):
        self.assertEqual(R.span_of(5), [5])

    def test_unparseable_is_flagged_not_swallowed(self):
        for bad in ("abc", "2-", "-3", "1\u20133", "1;3", "1,", "a-b", True,
                    {}, [], 2.5):
            self.assertIs(R.span_of(bad), R.BADPAGES, repr(bad))

    def test_describe_pages(self):
        self.assertEqual(R.describe_pages([1, 2, 3]), "1-3")
        self.assertEqual(R.describe_pages([2, 3, 4, 7]), "2-4,7")
        self.assertEqual(R.describe_pages([9]), "9")
        self.assertEqual(R.describe_pages([]), "")


class TestEstimates(unittest.TestCase):

    def test_cjk_counts_as_denser_tokens(self):
        self.assertAlmostEqual(R.est_text_tokens("abcd" * 100), 100, delta=2)
        self.assertAlmostEqual(R.est_text_tokens("中" * 100), 76, delta=2)

    def test_whitespace_is_free(self):
        """A blank scan extracts as a few newlines per page; that is not 20
        tokens of text, and the sidecar should not say it is."""
        self.assertEqual(R.est_text_tokens("\n\n \n" * 30), 0)
        self.assertEqual(R.est_text_tokens("ab cd"), R.est_text_tokens("abcd"))

    def test_cjk_ratio_beats_a_single_divisor(self):
        """The bug this replaces: chars//3 undercounts Chinese 2-3x."""
        cjk = "中文" * 500
        self.assertGreater(R.est_text_tokens(cjk), (len(cjk) // 3) * 1.9)

    def test_regex_token_estimate_matches_the_per_char_loop(self):
        """est_text_tokens was rewritten for speed; same numbers as before."""
        def old(text):
            text = "".join(ch for ch in text if not ch.isspace())
            cjk = sum(1 for ch in text if R.is_cjk(ch))
            return int(cjk / 1.3 + (len(text) - cjk) / 4.0)
        samples = ["", "abc", CJK, "a" + CJK + "b\n", "ｱｲｳ", "\uff61",
                   "  a b\tc\n\u3000d  ", "\n" * 80,
                   "\u4dbf\u4dc0", "\ud7af\ud7b0", "\uf900\uf8ff",
                   "".join(chr(c) for c in range(0x20, 0x3000, 7))]
        for s in samples:
            self.assertEqual(R.est_text_tokens(s), old(s), repr(s[:20]))

    def test_image_tokens_use_the_28px_grid(self):
        # A4 at 100 DPI: 826 x 1169 px -> ceil(826/28)=30 * ceil(1169/28)=42
        self.assertEqual(R.est_image_tokens(595, 842, 1, dpi=100), 30 * 42)

    def test_image_tokens_scale_with_pages(self):
        one = R.est_image_tokens(595, 842, 1, dpi=100)
        self.assertEqual(R.est_image_tokens(595, 842, 10, dpi=100), one * 10)

    def test_image_tokens_respect_the_long_edge_cap(self):
        uncapped = R.est_image_tokens(5000, 5000, 1, cap=None, token_cap=None)
        capped = R.est_image_tokens(5000, 5000, 1, cap=1568, token_cap=None)
        self.assertLess(capped, uncapped)
        self.assertEqual(capped, 56 * 56)

    def test_image_tokens_respect_the_per_image_cap(self):
        self.assertEqual(R.est_image_tokens(5000, 5000, 1), R.IMAGE_TOKEN_CAP)
        self.assertEqual(R.est_image_tokens(5000, 5000, 3), R.IMAGE_TOKEN_CAP * 3)

    def test_image_tokens_never_negative(self):
        self.assertGreaterEqual(R.est_image_tokens(-595, 842, 3, dpi=100), 0)
        self.assertEqual(R.est_image_tokens(595, 842, -4, dpi=100), 0)


class TestClean(unittest.TestCase):

    def test_normalises_newlines_and_strips_controls(self):
        out = R.clean("a\r\nb\r c\x00d\te")
        self.assertNotIn("\r", out)
        self.assertNotIn("\x00", out)
        self.assertIn("\t", out)

    def test_keeps_cjk(self):
        self.assertEqual(R.clean(CJK), CJK)

    def test_drops_private_use_glyphs(self):
        """A text layer made of U+F0xx is a symbol font with no encoding --
        a scan in disguise. Dropping it is what makes such a page blank."""
        self.assertEqual(R.clean("\uf041\uf042 x"), " x")

    def test_translate_table_matches_the_per_char_loop(self):
        """clean() was rewritten for speed; same output as before on every
        Unicode category, including unassigned and surrogate code points."""
        def old(text):
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            return "".join(
                c for c in text
                if c in "\n\t" or not unicodedata.category(c).startswith("C"))
        probe = "".join(chr(c) for c in range(0, 0x3000, 3))
        probe += "".join(chr(c) for c in (0xD800, 0xDFFF, 0xE000, 0xF8FF,
                                          0xFEFF, 0xFFFE, 0x10FFFF, 0xE0001,
                                          0x1F600, 0x20000, 0x2FFFF))
        probe += "\r\n\r" + CJK + "\x85\u2028\u200b"
        self.assertEqual(R.clean(probe), old(probe))


# --- extraction and encoding ----------------------------------------------


class TestEncoding(TempCache):
    """The whole pitch is 'extraction is safe for Chinese and Japanese'.
    An earlier suite passed with the writer switched to cp950, because every
    fixture was pure ASCII. These tests are what closes that hole."""

    def test_extracted_file_is_utf8_and_survives_a_roundtrip(self):
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        dest = os.path.join(R.CACHE_DIR, "cjk.txt")
        R.write_text_cache(dest, "src.pdf", [CJK, CJK + "第二節。"])
        with open(dest, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn(CJK, body)
        self.assertIn("第二節。", body)

    def test_extracted_bytes_are_not_a_legacy_codepage(self):
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        dest = os.path.join(R.CACHE_DIR, "cjk2.txt")
        R.write_text_cache(dest, "src.pdf", [CJK])
        with open(dest, "rb") as fh:
            raw = fh.read()
        self.assertIn(CJK.encode("utf-8"), raw)
        self.assertNotIn(CJK.encode("cp950"), raw)

    def test_line_count_is_consistent_between_write_and_reread(self):
        """Two code paths used to count lines differently, so the same file
        could be routed to Read once and to Grep the next time."""
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        dest = os.path.join(R.CACHE_DIR, "lines.txt")
        written = R.write_text_cache(dest, "src.pdf", [CJK, "", "tail\n\n"])
        self.assertEqual(written, R.count_lines(dest))

    def test_page_line_index_points_at_the_markers(self):
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        dest = os.path.join(R.CACHE_DIR, "idx.txt")
        meta = {}
        R.write_text_cache(dest, "src.pdf", ["one\ntwo", CJK, "x\n\ny"],
                           meta=meta)
        with open(dest, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        for page, ln in enumerate(meta["page_lines"], 1):
            self.assertTrue(lines[ln - 1].startswith("===== [page %d/3]" % page),
                            (page, ln, lines[ln - 1]))

    def test_page_classes_are_marked_in_the_text(self):
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        dest = os.path.join(R.CACHE_DIR, "blank.txt")
        R.write_text_cache(dest, "src.pdf", ["a" * 30, "", "", "c" * 86],
                           kinds=["text", "blank", "image", "figure"],
                           areas=[0.0, 0.0, 0.7, 0.452])
        with open(dest, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("===== [page 1/4] =====", body)
        self.assertIn("===== [page 2/4] (blank page) =====", body)
        self.assertIn("===== [page 3/4] (image page, no text layer) =====", body)
        self.assertIn("===== [page 4/4] (figure page: 45% image, 86 chars) =====",
                      body)

    def test_text_without_kinds_has_plain_markers(self):
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        dest = os.path.join(R.CACHE_DIR, "plain.txt")
        R.write_text_cache(dest, "src.pdf", ["a" * 30, ""])
        with open(dest, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("===== [page 2/2] =====", body)
        self.assertNotIn("page)", body)

    def test_deny_reason_is_ascii_even_for_a_cjk_path(self):
        payload = R.text_message(n=3, total=10, text_tokens=5, img_tokens=9,
                                 path_note="poppler path", native_works=False,
                                 dest="C:/暫存/研究.txt", lines=4)
        json.dumps({"r": payload}, ensure_ascii=True).encode("ascii")


# --- cache ------------------------------------------------------------------


class TestCacheKey(TempCache):

    def test_key_changes_with_size(self):
        a = self.text_file(1, "a.pdf")
        k1 = R.cache_key(a)
        with open(a, "ab") as fh:
            fh.write(b"%padding\n")
        self.assertNotEqual(k1, R.cache_key(a))

    def test_key_changes_when_content_changes_at_identical_size(self):
        """stat-based keys collide here: regenerating a PDF often lands in the
        same second at the same byte count, and the old text gets served."""
        a = fixtures.write(self.tmp, "same.pdf", fixtures.text_pdf(1))
        k1 = R.cache_key(a)
        st = os.stat(a)
        with open(a, "rb") as fh:
            data = bytearray(fh.read())
        data[-20:-17] = b"ZZZ"
        with open(a, "wb") as fh:
            fh.write(bytes(data))
        os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns))   # pin the mtime too
        self.assertEqual(len(bytes(data)), os.path.getsize(a))
        self.assertNotEqual(k1, R.cache_key(a))

    def test_key_changes_with_mtime_alone(self):
        a = self.text_file(1, "touch.pdf")
        k1 = R.cache_key(a)
        st = os.stat(a)
        os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 5000000000))
        self.assertNotEqual(k1, R.cache_key(a))

    def test_key_is_stable_for_an_untouched_file(self):
        a = self.text_file(1, "stable.pdf")
        self.assertEqual(R.cache_key(a), R.cache_key(a))

    def test_large_file_keying_samples_both_ends(self):
        head = b"%PDF-1.4\n" + b"A" * (R.SAMPLE_BYTES * 3)
        p1 = fixtures.write(self.tmp, "big1.pdf", head + b"TAIL-ONE")
        p2 = fixtures.write(self.tmp, "big2.pdf", head + b"TAIL-TWO")
        os.utime(p2, ns=(os.stat(p1).st_atime_ns, os.stat(p1).st_mtime_ns))
        self.assertNotEqual(R.cache_key(p1), R.cache_key(p2))

    def test_medium_file_is_hashed_whole(self):
        """Between one and two samples long, a change in the middle must
        still be seen."""
        size = int(R.SAMPLE_BYTES * 1.5)
        base = b"%PDF-1.4\n" + b"A" * size
        p1 = fixtures.write(self.tmp, "m1.pdf", base)
        mid = bytearray(base)
        mid[R.SAMPLE_BYTES + 100] = ord("B")
        p2 = fixtures.write(self.tmp, "m2.pdf", bytes(mid))
        os.utime(p2, ns=(os.stat(p1).st_atime_ns, os.stat(p1).st_mtime_ns))
        self.assertNotEqual(R.cache_key(p1), R.cache_key(p2))

    def test_name_is_filesystem_safe(self):
        self.assertNotIn("/", R.safe_name("/x/y/we ird:name?.pdf"))
        self.assertNotIn(" ", R.safe_name("we ird.pdf"))


class TestPartialFiles(TempCache):
    """Claude Code kills a hook that overruns its timeout, so half-written
    cache entries are a normal event. Trusting one means handing Claude a
    prefix of a document and letting it report on that as the whole thing."""

    def test_truncated_text_cache_is_rejected(self):
        path = self.text_file(3, "trunc.pdf")
        dest = R.text_cache_path(path)
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write("# Text extr")
        self.poppler(True)
        _, payload = R.decide(path, None)
        with open(payload.dest, encoding="utf-8") as fh:
            self.assertIn("===== [page 1/3]", fh.read())

    def test_empty_text_cache_is_rejected(self):
        path = self.text_file(3, "empty-cache.pdf")
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        open(R.text_cache_path(path), "w").close()
        self.poppler(True)
        _, payload = R.decide(path, None)
        self.assertGreater(payload.lines, 3)

    def test_zero_byte_png_is_rerendered(self):
        path = self.blank_file(12, "zero-png.pdf")
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        stale = R.image_cache_path(path, 2, R.RENDER_SCALE)
        open(stale, "w").close()
        self.poppler(False)
        _, payload = R.decide(path, None)
        page2 = dict(payload.imgs)[2]
        self.assertGreater(os.path.getsize(page2), 0)
        self.assertTrue(R.png_is_complete(page2))

    def test_no_temp_files_are_left_behind(self):
        self.poppler(False)
        R.decide(self.blank_file(12, "clean.pdf"), None)
        R.decide(self.text_file(4, "clean2.pdf"), None)
        leftovers = [f for f in os.listdir(R.CACHE_DIR) if ".tmp" in f]
        self.assertEqual(leftovers, [])

    def test_stale_temp_files_from_a_killed_hook_are_swept(self):
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        old = os.path.join(R.CACHE_DIR, "abc-x.txt.tmp999")
        fresh = os.path.join(R.CACHE_DIR, "abc-y.txt.tmp998")
        for p in (old, fresh):
            open(p, "w").close()
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        self.poppler(True)
        R.decide(self.text_file(2, "sweep.pdf"), None)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_complete_cache_is_reused_not_rewritten(self):
        """mtime cannot tell (a hit refreshes it on purpose), so plant a
        marker that only survives if the file was left alone."""
        self.poppler(True)
        path = self.text_file(3, "reuse.pdf")
        _, first = R.decide(path, None)
        with open(first.dest, "a", encoding="utf-8") as fh:
            fh.write("REUSED-MARKER")
        R.open_pdf = lambda _p: self.fail("PDF reopened on a cache hit")
        _, second = R.decide(path, None)
        self.assertEqual(first.dest, second.dest)
        with open(second.dest, encoding="utf-8") as fh:
            self.assertIn("REUSED-MARKER", fh.read())


class TestSidecar(TempCache):

    def test_sidecar_is_written_next_to_the_text(self):
        self.poppler(True)
        _, res = R.decide(self.text_file(3, "side.pdf"), None)
        with open(R.sidecar_path(res.dest), encoding="utf-8") as fh:
            meta = json.load(fh)
        for k in R.SIDECAR_KEYS:
            self.assertIn(k, meta)
        self.assertEqual(meta["pages"], 3)
        self.assertEqual(len(meta["per_page_chars"]), 3)
        self.assertEqual(meta["lines"], res.lines)
        self.assertEqual(meta["engine"], res.engine)
        self.assertEqual(len(meta["page_lines"]), 3)

    def test_cache_hit_never_opens_the_pdf(self):
        """With sidecar and text both present, the decision is made from
        the sidecar alone -- proven by making every engine explode."""
        self.poppler(True)
        path = self.text_file(3, "hit.pdf")
        _, first = R.decide(path, None)

        def boom(_path):
            raise AssertionError("PDF was opened on a cache hit")
        R.open_pdf = boom
        action, second = R.decide(path, None)
        self.assertEqual(action, "text")
        self.assertEqual(second.dest, first.dest)
        self.assertEqual(second.n, 3)
        self.assertEqual(second.lines, first.lines)
        self.assertEqual(second.page_lines, first.page_lines)

    def test_cache_hit_with_a_layout_request_still_renders(self):
        """The sidecar cannot render; a short range without poppler has to
        reopen the file."""
        self.poppler(True)
        path = self.text_file(6, "hit-render.pdf")
        R.decide(path, None)
        self.poppler(False)
        action, res = R.decide(path, "2-3")
        self.assertEqual(action, "render")
        self.assertEqual([p for p, _ in res.imgs], [2, 3])

    def test_a_hit_refreshes_the_cache_entry(self):
        """Pruning goes by mtime; a hit must count as use, or a file read
        every day still dies on day 30."""
        self.poppler(True)
        path = self.text_file(3, "lru.pdf")
        _, res = R.decide(path, None)
        old = time.time() - 20 * 86400
        for p in (res.dest, R.sidecar_path(res.dest)):
            os.utime(p, (old, old))
        R.decide(path, None)
        for p in (res.dest, R.sidecar_path(res.dest)):
            self.assertGreater(os.path.getmtime(p), old + 86400, p)

    def test_a_png_hit_refreshes_the_file(self):
        self.poppler(False)
        path = self.blank_file(12, "lru-png.pdf")
        _, res = R.decide(path, None)
        png = res.imgs[0][1]
        old = time.time() - 20 * 86400
        os.utime(png, (old, old))
        R.decide(path, None)
        self.assertGreater(os.path.getmtime(png), old + 86400)

    def test_sidecar_from_another_engine_is_not_trusted(self):
        """Engines disagree on glyphs (pypdf drops ~8% CJK on two-column
        layouts), so text extracted by one must not be served once a
        better one is installed."""
        self.poppler(True)
        path = self.text_file(3, "eng.pdf")
        _, first = R.decide(path, None)
        side = R.sidecar_path(first.dest)
        with open(side, encoding="utf-8") as fh:
            meta = json.load(fh)
        meta["engine"] = "pypdf"
        with open(side, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        with open(first.dest, "a", encoding="utf-8") as fh:
            fh.write("\nSTALE-MARKER\n")
        calls = []
        real = R.open_pdf

        def spy(p):
            calls.append(p)
            return real(p)
        R.open_pdf = spy
        _, second = R.decide(path, None)
        self.assertEqual(calls, [path])
        with open(first.dest, encoding="utf-8") as fh:
            self.assertNotIn("STALE-MARKER", fh.read())
        with open(side, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["engine"], R.active_engine_name())

    def rewrite_sidecar(self, dest, edit):
        side = R.sidecar_path(dest)
        with open(side, encoding="utf-8") as fh:
            meta = json.load(fh)
        edit(meta)
        with open(side, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write("\nSTALE-MARKER\n")

    def spy_open(self):
        calls, real = [], R.open_pdf

        def spy(p):
            calls.append(p)
            return real(p)
        R.open_pdf = spy
        return calls

    def test_old_sidecar_without_new_fields_is_re_extracted(self):
        """A sidecar from before the image measurement (or the raw count)
        cannot tell a blank page from a picture; the same mechanism that
        rejects another engine's sidecar rejects it, and the PDF is read
        again."""
        self.poppler(True)
        for field in ("page_image_area", "page_raw_chars"):
            path = self.mixed_file("old-%s.pdf" % field)
            _, first = R.decide(path, None)
            self.rewrite_sidecar(first.dest, lambda m: m.pop(field))
            calls = self.spy_open()
            _, second = R.decide(path, None)
            self.assertEqual(calls, [path], field)
            self.assertEqual(second.kinds, ["text", "blank", "image", "figure"])
            with open(second.dest, encoding="utf-8") as fh:
                self.assertNotIn("STALE-MARKER", fh.read())
            R.open_pdf = self._saved["open_pdf"]

    def test_sidecar_with_another_key_is_not_trusted(self):
        """In beside mode the file name carries no key, so this check is
        what stops a regenerated PDF from being served its old text."""
        self.poppler(True)
        path = self.text_file(3, "keyed.pdf")
        _, first = R.decide(path, None)
        self.rewrite_sidecar(first.dest, lambda m: m.update(key="000000000000"))
        calls = self.spy_open()
        R.decide(path, None)
        self.assertEqual(calls, [path])
        with open(first.dest, encoding="utf-8") as fh:
            self.assertNotIn("STALE-MARKER", fh.read())

    def test_text_without_sidecar_is_regenerated(self):
        self.poppler(True)
        path = self.text_file(3, "nosc.pdf")
        _, first = R.decide(path, None)
        os.remove(R.sidecar_path(first.dest))
        _, second = R.decide(path, None)
        self.assertTrue(os.path.isfile(R.sidecar_path(second.dest)))

    def test_corrupt_sidecar_is_ignored(self):
        self.poppler(True)
        path = self.text_file(3, "badsc.pdf")
        _, first = R.decide(path, None)
        with open(R.sidecar_path(first.dest), "w") as fh:
            fh.write("{not json")
        action, second = R.decide(path, None)
        self.assertEqual(action, "text")
        self.assertEqual(second.n, 3)


class TestPrune(TempCache):

    def fill(self, names, size=1000, age=0):
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        for i, name in enumerate(names):
            p = os.path.join(R.CACHE_DIR, name)
            with open(p, "wb") as fh:
                fh.write(b"x" * size)
            t = time.time() - age - (len(names) - i)   # earlier names are older
            os.utime(p, (t, t))

    def test_old_entries_are_dropped(self):
        self.fill(["old.txt", "old.json"], age=31 * 86400)
        self.fill(["new.png"])
        removed = R.prune_cache(max_bytes=10 ** 9)
        self.assertEqual(removed, 2)
        self.assertEqual(sorted(os.listdir(R.CACHE_DIR)), ["new.png"])

    def test_oldest_go_first_when_over_budget(self):
        self.fill(["a.txt", "b.txt", "c.png", "d.txt"], size=1000)
        R.prune_cache(max_bytes=2500)
        self.assertEqual(sorted(os.listdir(R.CACHE_DIR)), ["c.png", "d.txt"])

    def test_the_entry_just_written_is_kept(self):
        self.fill(["a.txt", "b.txt"], size=1000)
        R.prune_cache(max_bytes=500, keep=[os.path.join(R.CACHE_DIR, "a.txt")])
        self.assertEqual(os.listdir(R.CACHE_DIR), ["a.txt"])

    def test_budget_comes_from_the_environment(self):
        os.environ["PDF_TEXT_ROUTER_CACHE_MAX_MB"] = "0.001"    # ~1 KB
        try:
            self.fill(["a.txt", "b.txt", "c.txt"], size=600)
            self.poppler(True)
            R.decide(self.text_file(2, "budget.pdf"), None)
            left = [f for f in os.listdir(R.CACHE_DIR) if not f.startswith("budget")
                    and "budget.pdf" not in f]
            self.assertLessEqual(len(left), 1)
        finally:
            del os.environ["PDF_TEXT_ROUTER_CACHE_MAX_MB"]

    def test_clear_cache_cli(self):
        self.fill(["a.txt", "b.json", "c.png"], size=2048)
        env = dict(os.environ, PDF_TEXT_ROUTER_CACHE=R.CACHE_DIR)
        p = subprocess.run([sys.executable, HOOK, "--clear-cache"],
                           stdout=subprocess.PIPE, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"3 file(s)", p.stdout)
        self.assertEqual(os.listdir(R.CACHE_DIR), [])


class TestBeside(TempCache):
    """PDF_TEXT_ROUTER_CACHE=beside: cache files next to the PDF, in a
    folder that belongs to the user -- so nothing there is ever pruned."""

    def setUp(self):
        TempCache.setUp(self)
        R.CACHE_BESIDE = True
        self.docs = os.path.join(self.tmp, "docs")
        os.makedirs(self.docs)

    def doc(self, name, data):
        return fixtures.write(self.docs, name, data)

    def test_env_word_is_case_insensitive_and_a_path_is_not(self):
        for raw, want in (("beside", True), ("BESIDE", True), (" Beside ", True),
                          ("", False), (self.tmp, False), ("besides", False)):
            os.environ["PDF_TEXT_ROUTER_CACHE"] = raw
            try:
                d, beside = R._cache_setting()
            finally:
                del os.environ["PDF_TEXT_ROUTER_CACHE"]
            self.assertEqual(beside, want, raw)
            if want or not raw:
                self.assertEqual(d, R.DEFAULT_CACHE_DIR, raw)
            elif raw == self.tmp:
                self.assertEqual(d, os.path.normpath(self.tmp))

    def test_text_and_sidecar_land_next_to_the_pdf(self):
        self.poppler(True)
        path = self.doc("report.pdf", fixtures.text_pdf(3))
        action, res = R.decide(path, None)
        self.assertEqual(action, "text")
        self.assertEqual(res.dest, os.path.join(self.docs, "report.pdf.txt"))
        self.assertTrue(os.path.isfile(os.path.join(self.docs, "report.pdf.json")))
        self.assertFalse(os.path.exists(R.CACHE_DIR))
        with open(R.sidecar_path(res.dest), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["key"], R.cache_key(path))

    def test_a_hit_beside_the_pdf_never_reopens_it(self):
        self.poppler(True)
        path = self.doc("hit.pdf", fixtures.text_pdf(3))
        _, first = R.decide(path, None)
        R.open_pdf = lambda _p: self.fail("PDF reopened on a beside hit")
        _, second = R.decide(path, None)
        self.assertEqual(second.dest, first.dest)

    def test_a_regenerated_pdf_is_re_extracted(self):
        """No key in the name, so the sidecar's key is what catches it."""
        self.poppler(True)
        path = self.doc("regen.pdf", fixtures.text_pdf(3))
        _, first = R.decide(path, None)
        with open(path, "wb") as fh:
            fh.write(fixtures.text_pdf(4))
        _, second = R.decide(path, None)
        self.assertEqual(second.dest, first.dest)
        self.assertEqual(second.n, 4)
        with open(second.dest, encoding="utf-8") as fh:
            self.assertIn("[page 4/4]", fh.read())

    def test_rendered_pngs_land_next_to_the_pdf(self):
        self.poppler(False)
        path = self.doc("scan.pdf", fixtures.blank_pdf(12))
        action, res = R.decide(path, None)
        self.assertEqual(action, "render")
        self.assertEqual(res.imgs[0][1], os.path.join(
            self.docs, "scan.pdf.p1.%.1fx.png" % R.RENDER_SCALE))
        self.assertEqual(res.imgs[-1][1], os.path.join(
            self.docs, "scan.pdf.p10.%.1fx.png" % R.RENDER_SCALE))
        self.assertTrue(all(R.png_is_complete(f) for _, f in res.imgs))
        self.assertFalse(os.path.exists(R.CACHE_DIR))

    def test_a_new_scale_renders_a_new_png_beside(self):
        """Central names carry the scale; beside ones must too, or a new
        PDF_TEXT_ROUTER_SCALE serves the old rendering."""
        self.poppler(False)
        path = self.doc("scaled.pdf", fixtures.blank_pdf(12))
        eng = R.open_pdf(path)
        try:
            a = R.render_pages(eng, path, [2], scale=1.5)[0][1]
            b = R.render_pages(eng, path, [2], scale=2.0)[0][1]
        finally:
            eng.close()
        self.assertNotEqual(a, b)
        self.assertTrue(a.endswith("scaled.pdf.p2.1.5x.png"))
        self.assertTrue(b.endswith("scaled.pdf.p2.2.0x.png"))
        self.assertTrue(os.path.isfile(a) and os.path.isfile(b))

    def test_a_png_older_than_the_pdf_is_re_rendered(self):
        self.poppler(False)
        path = self.doc("stale.pdf", fixtures.blank_pdf(12))
        _, res = R.decide(path, "3")
        png = res.imgs[0][1]
        old = time.time() - 3600
        os.utime(png, (old, old))
        os.utime(path, None)
        R.decide(path, "3")
        self.assertGreater(os.path.getmtime(png), old + 1800)

    def test_unwritable_folder_falls_back_to_the_central_dir_once(self):
        R.dir_writable = lambda d: False
        self.poppler(True)
        path = self.doc("ro.pdf", fixtures.text_pdf(2))
        _, res = R.decide(path, None)
        self.assertTrue(res.dest.startswith(R.CACHE_DIR))
        self.assertEqual(sorted(os.listdir(self.docs)), ["ro.pdf"])
        R.decide(path, None)
        R.decide(self.doc("ro2.pdf", fixtures.text_pdf(2)), None)
        self.assertEqual(self.stderr().count("not writable"), 1)

    def test_permission_error_on_write_falls_back_to_the_central_dir(self):
        """os.access says yes to many folders Windows then refuses."""
        real = R.atomic_write

        def refuse(dest, data, binary=False):
            if not R.in_central(dest):
                raise PermissionError(13, "Access is denied", dest)
            real(dest, data, binary)
        R.atomic_write = refuse
        self.poppler(True)
        path = self.doc("perm.pdf", fixtures.text_pdf(2))
        action, res = R.decide(path, None)
        self.assertEqual(action, "text")
        self.assertTrue(res.dest.startswith(R.CACHE_DIR))
        self.assertTrue(os.path.isfile(R.sidecar_path(res.dest)))
        self.assertIn("cannot write in", self.stderr())
        # and the central copy is found next time without a new extraction
        R.open_pdf = lambda _p: self.fail("PDF reopened despite a central hit")
        _, again = R.decide(path, None)
        self.assertEqual(again.dest, res.dest)

    def test_permission_error_on_render_falls_back_to_the_central_dir(self):
        class Refusing(R.Pdfium):
            def render(self, index, scale, dest):
                if not R.in_central(dest):
                    raise PermissionError(13, "Access is denied", dest)
                return R.Pdfium.render(self, index, scale, dest)
        R.ENGINES = (Refusing,)
        self.poppler(False)
        path = self.doc("perm-scan.pdf", fixtures.blank_pdf(12))
        action, res = R.decide(path, "2")
        self.assertEqual(action, "render")
        self.assertTrue(res.imgs[0][1].startswith(R.CACHE_DIR))
        self.assertEqual(sorted(os.listdir(self.docs)), ["perm-scan.pdf"])

    def test_nothing_in_the_users_folder_is_pruned_or_swept(self):
        calls = []
        R.prune_cache = lambda *a, **k: calls.append(a) or 0
        os.environ["PDF_TEXT_ROUTER_CACHE_MAX_MB"] = "0.001"
        try:
            old = time.time() - 40 * 86400
            bystanders = ["notes.tmp999", "big.png", "other.pdf.txt"]
            for name in bystanders:
                p = os.path.join(self.docs, name)
                with open(p, "wb") as fh:
                    fh.write(b"x" * 4096)
                os.utime(p, (old, old))
            self.poppler(False)
            R.decide(self.doc("mine.pdf", fixtures.text_pdf(2)), None)
            R.decide(self.doc("scan.pdf", fixtures.blank_pdf(12)), None)
        finally:
            del os.environ["PDF_TEXT_ROUTER_CACHE_MAX_MB"]
        self.assertEqual(calls, [])
        for name in bystanders:
            p = os.path.join(self.docs, name)
            self.assertTrue(os.path.isfile(p), name)
            self.assertLess(os.path.getmtime(p), old + 60, name)

    def test_prune_still_runs_after_a_fallback_to_the_central_dir(self):
        calls = []
        R.prune_cache = lambda *a, **k: calls.append(a) or 0
        R.dir_writable = lambda d: False
        self.poppler(True)
        R.decide(self.doc("fb.pdf", fixtures.text_pdf(2)), None)
        self.assertEqual(len(calls), 1)

    def test_clear_cache_deletes_nothing_beside(self):
        path = self.doc("keep.pdf", fixtures.text_pdf(2))
        self.poppler(True)
        R.decide(path, None)
        before = sorted(os.listdir(self.docs))
        self.assertIn("keep.pdf.txt", before)
        env = dict(os.environ, PDF_TEXT_ROUTER_CACHE="beside")
        p = subprocess.run([sys.executable, HOOK, "--clear-cache"],
                           stdout=subprocess.PIPE, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"Nothing was deleted", p.stdout)
        self.assertEqual(sorted(os.listdir(self.docs)), before)

    def test_selftest_names_the_mode(self):
        for raw, want in (("beside", b"cache mode       beside"),
                          (R.CACHE_DIR, b"cache mode       central")):
            env = dict(os.environ, PDF_TEXT_ROUTER_CACHE=raw)
            p = subprocess.run([sys.executable, HOOK, "--selftest"],
                               stdout=subprocess.PIPE, env=env)
            self.assertEqual(p.returncode, 0, raw)
            self.assertIn(want, p.stdout)

    def test_hook_process_writes_beside(self):
        path = self.doc("proc.pdf", fixtures.text_pdf(3))
        env = dict(os.environ, PDF_TEXT_ROUTER_CACHE="beside")
        env.pop("PDF_TEXT_ROUTER_MODE", None)
        p = subprocess.run([sys.executable, HOOK],
                           input=json.dumps({"tool_name": "Read",
                                             "tool_input": {"file_path": path}}).encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        d = json.loads(p.stdout.decode())["hookSpecificOutput"]
        self.assertEqual(d["updatedInput"]["file_path"],
                         os.path.join(self.docs, "proc.pdf.txt"))
        self.assertEqual(p.stderr, b"")


# --- engines ----------------------------------------------------------------


class TestEngines(TempCache):

    def only(self, *classes):
        R.ENGINES = tuple(classes)

    def test_pdfium_is_preferred(self):
        self.assertIs(R.ENGINES[0], R.Pdfium)

    def test_each_engine_reads_the_same_document(self):
        path = self.text_file(3, "engines.pdf")
        seen = {}
        for cls in (R.Pdfium, R.MuPdf, R.PyPdf):
            try:
                eng = cls(path)
            except ImportError:
                continue
            try:
                seen[cls.name] = (eng.page_count(),
                                  "p1" in "".join(eng.texts()))
            finally:
                eng.close()
        self.assertTrue(seen)
        for name, (pages, has_text) in seen.items():
            self.assertEqual(pages, 3, name)
            self.assertTrue(has_text, name)

    def test_extract_only_engine_never_renders(self):
        self.only(R.PyPdf)
        self.poppler(False)
        action, reason = R.decide(self.blank_file(12, "pypdf.pdf"), None)
        self.assertEqual(action, "allow")
        self.assertIn("cannot render", reason)

    def test_no_engine_at_all_fails_open(self):
        self.only()
        self.assertEqual(R.decide(self.text_file(2), None)[0], "allow")

    def test_a_failing_engine_hands_over_to_the_next(self):
        """A parse failure in pdfium used to end the search; now MuPDF (or
        pypdf) gets its turn, and the failure is logged on stderr."""
        def broken(self, path):
            raise RuntimeError("simulated pdfium parse failure")
        R.Pdfium.__init__ = broken
        self.poppler(True)
        action, res = R.decide(self.text_file(3, "handover.pdf"), None)
        self.assertEqual(action, "text")
        self.assertIn(res.engine, ("pymupdf", "pypdf"))
        self.assertIn("pypdfium2 failed on handover.pdf: RuntimeError", self.stderr())

    def test_all_engines_failing_is_an_allow_with_a_log_line(self):
        action, _ = R.decide(fixtures.write(self.tmp, "j.pdf", b"nope"), None)
        self.assertEqual(action, "allow")
        self.assertIn("pdf-text-router:", self.stderr())

    def test_an_exception_after_open_is_logged_and_allowed(self):
        class Exploding(R.Pdfium):
            def texts(self):
                raise ValueError("bad glyph table")
        self.only(Exploding)
        action, reason = R.decide(self.text_file(2, "explode.pdf"), None)
        self.assertEqual(action, "allow")
        self.assertIn("ValueError", reason)
        self.assertIn("giving up on explode.pdf: ValueError", self.stderr())

    def test_pdfium_releases_the_file_handle(self):
        """Windows refuses to delete a PDF that pdfium still holds open."""
        path = self.text_file(1, "handle.pdf")
        self.poppler(True)
        R.decide(path, None)
        os.remove(path)
        self.assertFalse(os.path.exists(path))


class TestEncryption(TempCache):

    def engines(self, path):
        out = {}
        for cls in (R.Pdfium, R.MuPdf, R.PyPdf):
            try:
                eng = cls(path)
            except ImportError:
                continue
            try:
                out[cls.name] = bool(eng.encrypted)
            finally:
                eng.close()
        return out

    def test_user_password_is_encrypted_in_every_engine(self):
        path = fixtures.write(self.tmp, "upw.pdf",
                              fixtures.encrypted_pdf(fixtures.text_pdf(2), "secret"))
        seen = self.engines(path)
        self.assertTrue(seen)
        self.assertTrue(all(seen.values()), seen)

    def test_owner_password_only_is_readable_in_every_engine(self):
        path = fixtures.write(self.tmp, "opw.pdf",
                              fixtures.encrypted_pdf(fixtures.text_pdf(2), ""))
        seen = self.engines(path)
        self.assertTrue(seen)
        self.assertFalse(any(seen.values()), seen)

    def test_user_password_file_is_allowed_through(self):
        path = fixtures.write(self.tmp, "upw2.pdf",
                              fixtures.encrypted_pdf(fixtures.text_pdf(2), "pw"))
        action, reason = R.decide(path, None)
        self.assertEqual(action, "allow")
        self.assertIn("encrypted", reason)
        self.assertEqual(self.stderr(), "")

    def test_owner_password_file_is_extracted(self):
        self.poppler(True)
        path = fixtures.write(self.tmp, "opw2.pdf",
                              fixtures.encrypted_pdf(fixtures.text_pdf(2), ""))
        action, res = R.decide(path, None)
        self.assertEqual(action, "text")
        self.assertGreater(res.total, 100)


# --- classification ---------------------------------------------------------


class TestClassification(TempCache):

    def test_one_text_page_in_twenty_is_a_scan(self):
        path = self.text_file(20, "mostly-blank.pdf", blank=range(2, 21))
        self.poppler(True)
        action, reason = R.decide(path, None)
        self.assertEqual(action, "allow")
        self.assertIn("scan", reason)

    def test_one_blank_page_in_twenty_is_text_and_named(self):
        """An empty page (no text, no image) is something to skip, not to
        look at: the note says so and offers no pages= hint for it."""
        path = self.text_file(20, "one-blank.pdf", blank=[7])
        self.poppler(True)
        action, res = R.decide(path, None)
        self.assertEqual(action, "text")
        self.assertEqual(res.blank_pages, [7])
        self.assertEqual(res.image_pages, [])
        msg = R.text_message(res.n, res.total, res.text_tokens, res.img_tokens,
                             res.path_note, res.native_works, res.dest, res.lines,
                             res.kinds)
        self.assertIn("Page 7 of 20 is blank (no text, no image); skip it.", msg)
        self.assertNotIn('pages="7"', msg)
        with open(res.dest, encoding="utf-8") as fh:
            self.assertIn("[page 7/20] (blank page)", fh.read())

    def test_half_blank_is_still_text(self):
        path = self.text_file(10, "half.pdf", blank=[1, 2, 3, 4, 5])
        _, res = R.decide(path, None, dry_run=True)
        self.assertEqual(res.blank_pages, [1, 2, 3, 4, 5])

    def test_private_use_text_layer_is_a_scan(self):
        """Symbol-font PDFs extract as U+F0xx: characters, but not text."""
        path = self.text_file(2, "pua.pdf", pua=True)
        eng = R.open_pdf(path)
        try:
            self.assertIn("\uf054\uf068\uf065", "".join(eng.texts()))   # "The"
            meta, _ = R.inspect_pdf(eng)
        finally:
            eng.close()
        kinds, scanned = R.classify(meta)
        self.assertTrue(scanned)
        self.assertEqual(kinds, ["image", "image"])     # unreadable, not empty
        self.assertEqual(meta["per_page_chars"], [0, 0])
        self.assertGreater(meta["page_raw_chars"][0], R.BLANK_PAGE_CHARS)
        self.assertEqual(R.decide(path, None)[0], "allow")

    def test_a_symbol_font_page_in_a_text_document_is_marked_unreadable(self):
        """One PUA page among real ones must not be called blank: the
        reader would skip a page that has words on it."""
        pua = fixtures.write(self.tmp, "pua1.pdf", fixtures.text_pdf(1, pua=True))
        eng = R.open_pdf(pua)
        try:
            raw = eng.texts()[0]
        finally:
            eng.close()
        self.assertGreater(len(raw.strip()), R.BLANK_PAGE_CHARS)
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        dest = os.path.join(R.CACHE_DIR, "unreadable.txt")
        R.write_text_cache(dest, "src.pdf", ["a" * 30, R.clean(raw), ""],
                           kinds=["text", "image", "blank"],
                           areas=[0.0, 0.0, 0.0],
                           raw_chars=[30, len(raw.strip()), 0])
        with open(dest, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("===== [page 2/3] (image page, text layer unreadable) =====",
                      body)
        self.assertIn("===== [page 3/3] (blank page) =====", body)

    def test_page_list_is_capped_per_class(self):
        kinds = kinds_with(60, blank=range(1, 31), image=range(31, 56))
        blank, image = R.page_kinds_note(kinds, 60)
        self.assertIn("and 10 more", blank)
        self.assertNotIn(" 21,", blank)
        self.assertIn("and 5 more", image)
        self.assertEqual(R.page_kinds_note(["text"] * 5, 5), [])

    # -- the four page classes (F2) -----------------------------------------

    def test_page_kind_rules(self):
        k = R.page_kind
        self.assertEqual(k(0, 0.0), "blank")
        self.assertEqual(k(14, 0.049), "blank")
        self.assertEqual(k(0, 0.05), "image")
        self.assertEqual(k(1, 0.70), "image")            # zenodo 22070313 p9
        self.assertEqual(k(86, 0.45), "figure")          # zenodo 22070313 p8
        self.assertEqual(k(199, 0.40), "figure")
        self.assertEqual(k(200, 0.99), "text")           # enough words to read
        self.assertEqual(k(86, 0.39), "text")
        self.assertEqual(k(41, 0.02), "text")            # vector map: accepted miss
        self.assertEqual(k(15, 0.0), "text")
        # a text layer clean() emptied is unreadable, not blank
        self.assertEqual(k(0, 0.0, raw_chars=0), "blank")
        self.assertEqual(k(0, 0.0, raw_chars=14), "blank")
        self.assertEqual(k(0, 0.0, raw_chars=15), "image")
        self.assertEqual(k(14, 0.0, raw_chars=500), "image")
        self.assertEqual(k(0, None, raw_chars=0), "image")
        self.assertTrue(R.unreadable(0, 15))
        self.assertFalse(R.unreadable(0, None))
        self.assertFalse(R.unreadable(20, 20))

    def test_unknown_image_area_falls_back_to_chars_alone(self):
        """No measurement (pypdf): a charless page is called an image page,
        so Claude looks rather than skips; nothing can be a figure."""
        self.assertEqual(R.page_kind(0, None), "image")
        self.assertEqual(R.page_kind(14, None), "image")
        self.assertEqual(R.page_kind(15, None), "text")
        self.assertEqual(R.page_kind(86, None), "text")
        meta = {"pages": 3, "chars": 300, "per_page_chars": [300, 0, 0],
                "page_image_area": [None, None, None]}
        kinds, scanned = R.classify(meta)
        self.assertEqual(kinds, ["text", "image", "image"])
        self.assertTrue(scanned)
        meta_without = {"pages": 2, "chars": 300, "per_page_chars": [300, 0]}
        self.assertEqual(R.classify(meta_without)[0], ["text", "image"])

    def test_image_share_is_clamped_and_never_negative(self):
        self.assertEqual(R._image_share([(0, 0, 595, 842), (0, 0, 100, 100)],
                                        595, 842), 1.0)
        self.assertEqual(R._image_share([(100, 100, 50, 50)], 595, 842), 0.0)
        self.assertIsNone(R._image_share([(0, 0, 10, 10)], 0, 842))
        self.assertAlmostEqual(R._image_share([(0, 0, 297.5, 842)], 595, 842), 0.5)

    def test_mixed_document_gets_one_class_per_page(self):
        self.poppler(True)
        action, res = R.decide(self.mixed_file(), None)
        self.assertEqual(action, "text")
        self.assertEqual(res.kinds, ["text", "blank", "image", "figure"])
        self.assertEqual((res.blank_pages, res.image_pages, res.figure_pages),
                         ([2], [3], [4]))
        with open(R.sidecar_path(res.dest), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(len(meta["page_image_area"]), 4)
        self.assertAlmostEqual(meta["page_image_area"][2], 0.7, places=2)
        self.assertAlmostEqual(meta["page_image_area"][3], 0.7, places=2)
        self.assertEqual(meta["page_image_count"], [0, 0, 1, 1])
        with open(res.dest, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("===== [page 1/4] =====", body)
        self.assertIn("===== [page 2/4] (blank page) =====", body)
        self.assertIn("===== [page 3/4] (image page, no text layer) =====", body)
        self.assertIn("===== [page 4/4] (figure page: 70% image, 47 chars) =====",
                      body)

    def test_mixed_document_context_lists_the_three_classes_apart(self):
        self.poppler(True)
        _, res = R.decide(self.mixed_file(), None)
        for text in (R.text_context(res),
                     R.text_message(res.n, res.total, res.text_tokens,
                                    res.img_tokens, res.path_note,
                                    res.native_works, res.dest, res.lines,
                                    res.kinds)):
            self.assertIn("Page 2 of 4 is blank (no text, no image); skip it.",
                          text)
            self.assertIn("Page 3 of 4 has no readable text layer", text)
            self.assertIn('pages="3" to see one', text)
            self.assertIn("Page 4 of 4 is mostly picture with little text", text)
            self.assertIn("may miss what the figure says", text)
            self.assertIn('pages="4" to see it', text)

    def test_every_engine_agrees_on_the_mixed_document(self):
        path = self.mixed_file()
        for cls in (R.Pdfium, R.MuPdf):
            try:
                eng = cls(path)
            except ImportError:
                continue
            try:
                meta, _ = R.inspect_pdf(eng)
            finally:
                eng.close()
            self.assertEqual(R.classify(meta)[0],
                             ["text", "blank", "image", "figure"], cls.name)
            self.assertAlmostEqual(meta["page_image_area"][2], 0.7, places=2,
                                   msg=cls.name)
        eng = R.PyPdf(path)
        try:
            meta, _ = R.inspect_pdf(eng)
        finally:
            eng.close()
        self.assertEqual(meta["page_image_area"], [None] * 4)
        self.assertEqual(R.classify(meta)[0], ["text", "image", "image", "text"])

    def test_pypdf_only_routing_uses_the_old_rule(self):
        R.ENGINES = (R.PyPdf,)
        self.poppler(True)
        action, res = R.decide(self.mixed_file(), None)
        self.assertEqual(action, "text")
        self.assertEqual(res.kinds, ["text", "image", "image", "text"])
        self.assertEqual(res.blank_pages, [])
        self.assertEqual(res.figure_pages, [])

    def test_a_page_whose_image_scan_explodes_is_unknown_not_fatal(self):
        class Flaky(R.Pdfium):
            def page_images(self, i):
                if i == 2:
                    raise RuntimeError("bad image dictionary")
                return R.Pdfium.page_images(self, i)
        R.ENGINES = (Flaky,)
        self.poppler(True)
        action, res = R.decide(self.mixed_file(), None)
        self.assertEqual(action, "text")
        self.assertEqual(res.kinds, ["text", "blank", "image", "figure"])
        with open(R.sidecar_path(res.dest), encoding="utf-8") as fh:
            self.assertIsNone(json.load(fh)["page_image_area"][2])
        self.assertIn("image scan failed on 1 page(s)", self.stderr())

    def test_scan_rule_ignores_the_image_measurement(self):
        """Ten charless pages are a scan whether they carry images or not."""
        for name, images in (("s1.pdf", None), ("s2.pdf", dict((p, 0.9) for p in
                                                                range(1, 11)))):
            path = fixtures.write(self.tmp, name, fixtures.text_pdf(
                10, blank=range(1, 11), images=images))
            self.poppler(True)
            action, reason = R.decide(path, None)
            self.assertEqual(action, "allow", name)
            self.assertIn("scan", reason)


# --- routing ----------------------------------------------------------------


class TestRouting(TempCache):

    def test_text_layer_whole_file_extracts(self):
        self.poppler(True)
        action, payload = R.decide(self.text_file(2), None)
        self.assertEqual(action, "text")
        self.assertTrue(os.path.isfile(payload.dest))

    def test_extracted_text_carries_page_markers(self):
        self.poppler(True)
        _, payload = R.decide(self.text_file(3), None)
        with open(payload.dest, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("[page 1/3]", body)
        self.assertIn("[page 3/3]", body)

    def test_short_scan_allowed_with_poppler(self):
        self.poppler(True)
        self.assertEqual(R.decide(self.blank_file(3), None)[0], "allow")

    def test_short_scan_allowed_without_poppler(self):
        """<= 10 pages goes to the API as a document block; poppler is only
        reached for page-range reads. Verified against a live Read."""
        self.poppler(False)
        self.assertEqual(R.decide(self.blank_file(3), None)[0], "allow")

    def test_long_scan_allowed_when_poppler_is_present(self):
        """Same input as the next test, opposite flag, opposite outcome --
        which is what makes the flag load-bearing."""
        self.poppler(True)
        self.assertEqual(R.decide(self.blank_file(12), None)[0], "allow")

    def test_long_scan_renders_the_first_ten_without_poppler(self):
        """Ten is Read's own whole-file budget; the rest is on request."""
        self.poppler(False)
        action, res = R.decide(self.blank_file(12), None)
        self.assertEqual(action, "render")
        self.assertEqual([p for p, _ in res.imgs], list(range(1, 11)))
        self.assertEqual(res.rest, 2)
        for _, path in res.imgs:
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(R.png_is_complete(path))

    def test_scan_with_a_range_renders_up_to_twenty(self):
        self.poppler(False)
        action, res = R.decide(self.blank_file(25), "1-25")
        self.assertEqual(action, "render")
        self.assertEqual(len(res.imgs), R.MAX_RENDER_PAGES)
        self.assertEqual(res.rest, 5)

    def test_short_range_allowed_when_poppler_present(self):
        self.poppler(True)
        self.assertEqual(R.decide(self.text_file(6), "2-3")[0], "allow")

    def test_short_range_renders_locally_without_poppler(self):
        """The old hook told Claude to retry down the one path that cannot work."""
        self.poppler(False)
        action, res = R.decide(self.text_file(6), "2-3")
        self.assertEqual(action, "render")
        self.assertEqual([p for p, _ in res.imgs], [2, 3])

    def test_non_contiguous_range_renders_exactly_those_pages(self):
        self.poppler(False)
        action, res = R.decide(self.text_file(6), "1,4")
        self.assertEqual(action, "render")
        self.assertEqual([p for p, _ in res.imgs], [1, 4])

    def test_wide_range_is_a_read_not_a_look(self):
        self.poppler(True)
        self.assertEqual(R.decide(self.text_file(20), "1-15")[0], "text")

    def test_whole_document_range_is_a_read_not_a_look(self):
        """"1-2" on a two-page file is the whole file; only on a longer one
        does the same range mean 'show me these two pages'."""
        self.poppler(False)
        self.assertEqual(R.decide(self.text_file(2, "two.pdf"), "1-2")[0], "text")
        self.assertEqual(R.decide(self.text_file(50, "fifty.pdf"), "1-2")[0],
                         "render")

    def test_range_on_text_is_billed_on_those_pages(self):
        self.poppler(True)
        _, whole = R.decide(self.text_file(20, "bill.pdf"), None, dry_run=True)
        _, part = R.decide(self.text_file(20, "bill.pdf"), "1-10", dry_run=True)
        self.assertEqual(part.requested, list(range(1, 11)))
        self.assertLess(part.text_tokens, whole.text_tokens)
        self.assertLess(part.img_tokens, whole.img_tokens)
        self.assertAlmostEqual(part.text_tokens * 2, whole.text_tokens, delta=20)

    def test_absurd_range_is_out_of_range_even_with_poppler(self):
        self.poppler(True)
        action, payload = R.decide(self.text_file(3), "1-999999")
        self.assertEqual(action, "outofrange")
        self.assertEqual(payload, (4, 3))

    def test_out_of_range_is_reported_not_waved_through(self):
        """Waving it through sends Claude at the one path missing poppler."""
        self.poppler(False)
        for bad in ("0", "9-2", "99-100", "4-6", "1,5"):
            action, payload = R.decide(self.blank_file(3), bad)
            self.assertEqual(action, "outofrange", bad)
            self.assertEqual(payload[1], 3)

    def test_partly_out_of_range_is_out_of_range(self):
        """"11-13" on a 12-page file used to be clamped; now it is an error,
        the same answer Read itself gives when poppler is present."""
        self.poppler(False)
        action, payload = R.decide(self.blank_file(12), "11-13")
        self.assertEqual(action, "outofrange")
        self.assertEqual(payload, (13, 12))

    def test_unparseable_pages_is_its_own_action(self):
        self.poppler(True)
        for bad in ("2-", "1\u20133", "x", "1;2"):
            action, payload = R.decide(self.text_file(3), bad)
            self.assertEqual(action, "badpages", bad)
            self.assertEqual(payload, (bad, 3))
        msg = R.badpages_message("2-", 3)
        self.assertIn("N, N-M or a comma list", msg)
        self.assertIn("3 pages", msg)

    def test_zero_byte_file_fails_open(self):
        self.assertEqual(
            R.decide(fixtures.write(self.tmp, "e.pdf", b""), None)[0], "allow")

    def test_garbage_file_fails_open(self):
        self.assertEqual(
            R.decide(fixtures.write(self.tmp, "j.pdf", b"nope"), None)[0], "allow")

    def test_dry_run_writes_nothing(self):
        self.poppler(False)
        R.decide(self.text_file(4), None, dry_run=True)
        R.decide(self.blank_file(12), None, dry_run=True)
        self.assertFalse(os.path.isdir(R.CACHE_DIR))

    def test_render_scale_env_is_clamped(self):
        for raw, want in (("2.0", 2.0), ("9", 3.0), ("0.1", 1.0), ("x", 1.5),
                          ("", 1.5)):
            os.environ["PDF_TEXT_ROUTER_SCALE"] = raw
            try:
                self.assertEqual(R._scale_from_env(), want, raw)
            finally:
                del os.environ["PDF_TEXT_ROUTER_SCALE"]


# --- messages and rewrite inputs ------------------------------------------


class TestMessages(TempCache):

    def base(self, lines, tokens):
        return R.text_message(n=50, total=99999, text_tokens=tokens,
                              img_tokens=tokens * 3, path_note="poppler path",
                              native_works=False, dest="X.txt", lines=lines)

    def test_normal_message_asks_for_a_read(self):
        msg = self.base(lines=100, tokens=5000)
        self.assertIn("call Read on that .txt", msg)
        self.assertNotIn("Grep", msg)

    def test_long_output_asks_for_grep(self):
        msg = self.base(lines=R.MAX_READ_LINES + 1, tokens=5000)
        self.assertIn("Grep", msg)
        self.assertIn("offset/limit", msg)

    def test_token_heavy_output_asks_for_grep(self):
        self.assertIn("Grep", self.base(lines=10,
                                        tokens=R.LARGE_TEXT_TOKENS + 1))

    def test_broken_native_path_is_stated_plainly(self):
        self.assertIn("only path that works", self.base(lines=10, tokens=100))

    def test_working_native_path_offers_the_escape_hatch(self):
        msg = R.text_message(n=4, total=900, text_tokens=200, img_tokens=900,
                             path_note="API document block", native_works=True,
                             dest="X.txt", lines=20)
        self.assertIn("pages=", msg)
        self.assertIn("700 spent on pictures of words", msg)

    def test_text_dearer_than_images_is_said_honestly(self):
        msg = R.text_message(n=1, total=9000, text_tokens=3000, img_tokens=1260,
                             path_note="poppler path", native_works=True,
                             dest="X.txt", lines=20)
        self.assertIn("not cheaper here", msg)
        self.assertIn("exact, greppable", msg)
        self.assertNotIn("pictures of words", msg)

    def test_page_classes_are_listed_apart(self):
        msg = R.text_message(n=110, total=9000, text_tokens=3000, img_tokens=99999,
                             path_note="poppler path", native_works=True,
                             dest="X.txt", lines=20,
                             kinds=kinds_with(110, blank=[38, 76], image=[9, 12],
                                              figure=[8]))
        self.assertIn("Pages 38, 76 of 110 are blank (no text, no image); "
                      "skip them.", msg)
        self.assertIn("Pages 9, 12 of 110 have no readable text layer", msg)
        self.assertIn('pages="9" to see one', msg)
        self.assertIn("Page 8 of 110 is mostly picture with little text", msg)
        self.assertIn('pages="8" to see it', msg)
        self.assertNotIn('pages="38"', msg)

    def test_blank_only_note_offers_no_look(self):
        notes = R.page_kinds_note(kinds_with(5, blank=[2]), 5)
        self.assertEqual(len(notes), 1)
        self.assertNotIn("pages=", notes[0])

    def test_render_message_names_every_file(self):
        msg = R.render_message(2, 2, 9, True, [(2, "a.png"), (3, "b.png")])
        self.assertIn("a.png", msg)
        self.assertIn("b.png", msg)
        self.assertIn("pdftoppm", msg)

    def test_render_context_marks_the_page_this_read_shows(self):
        res = R.RenderResult(2, 2, 9, True, [(2, "a.png"), (3, "b.png")],
                             [2, 3], 0, "x.pdf")
        ctx = R.render_context(res)
        self.assertIn("a.png  (this Read)", ctx)
        self.assertIn("b.png", ctx)
        self.assertIn("pdftoppm", ctx)

    def test_text_context_keeps_file_and_range_tokens_apart(self):
        res = R.TextResult(n=110, total=9000, text_tokens=1100, img_tokens=5000,
                           path_note="poppler path", native_works=False,
                           engine="pypdfium2", dest="X.txt", lines=4000,
                           kinds=kinds_with(110, image=[38, 76]),
                           requested=list(range(1, 41)),
                           page_lines=None, src="x.pdf", file_tokens=52000)
        ctx = R.text_context(res, "positioned at line 4 (page 1)")
        self.assertIn("~52,000 tokens)", ctx)                # the whole file
        self.assertIn("Pages 1-40 as text: ~1,100 tokens", ctx)
        self.assertIn("Pages 38, 76 of 110 have no readable text layer", ctx)
        self.assertIn("[page N/110]", ctx)
        self.assertIn("positioned at line 4", ctx)


class TestRewriteInput(unittest.TestCase):

    def res(self, **kw):
        base = dict(n=5, total=5000, text_tokens=1000, img_tokens=6000,
                    path_note="poppler path", native_works=True, engine="pypdfium2",
                    dest="X.txt", lines=100, kinds=["text"] * 5, requested=None,
                    page_lines=[5, 25, 45, 65, 85], src="x.pdf",
                    file_tokens=1000)
        if "text_tokens" in kw and "file_tokens" not in kw:
            base["file_tokens"] = kw["text_tokens"]
        base.update(kw)
        return R.TextResult(**base)

    def test_small_file_gets_just_the_path(self):
        upd, note = R.text_rewrite_input({}, self.res())
        self.assertEqual(upd, {"file_path": "X.txt"})
        self.assertIsNone(note)

    def test_large_file_gets_a_preview_limit(self):
        """The limit is a token budget now, so a sparse file (1,000 tokens
        over 5,000 lines) hits the line ceiling and a dense one the floor."""
        upd, note = R.text_rewrite_input({}, self.res(lines=5000))
        self.assertEqual(upd["limit"], R.PREVIEW_LINES)
        self.assertNotIn("offset", upd)
        self.assertIn("first %d lines" % R.PREVIEW_LINES, note)
        self.assertIn("%d tokens" % R.PREVIEW_TOKENS, note)
        upd, _ = R.text_rewrite_input({}, self.res(text_tokens=R.LARGE_TEXT_TOKENS + 1))
        self.assertEqual(upd["limit"], R.PREVIEW_LINES_MIN)

    def test_callers_own_offset_or_limit_is_left_alone(self):
        for ti in ({"offset": 300}, {"limit": 50}, {"offset": 1, "limit": 9}):
            ti = dict(ti, file_path="x.pdf")
            upd, note = R.text_rewrite_input(ti, self.res(lines=5000, requested=[2]))
            self.assertEqual(upd, {"file_path": "X.txt"}, ti)
            self.assertIsNone(note)

    def test_page_range_positions_the_read_at_the_marker(self):
        upd, note = R.text_rewrite_input({}, self.res(requested=[2, 3]))
        self.assertEqual(upd["offset"], 24)             # one line before page 2
        self.assertEqual(upd["limit"], 64 - 24)         # up to just before page 4
        self.assertIn("page 2", note)

    def test_last_page_runs_to_the_end_of_file(self):
        upd, _ = R.text_rewrite_input({}, self.res(requested=[5]))
        self.assertEqual(upd["offset"], 84)
        self.assertEqual(upd["limit"], 101 - 84)

    def test_a_huge_range_is_capped_at_the_read_limit(self):
        upd, note = R.text_rewrite_input(
            {}, self.res(lines=9000, page_lines=[5, 3000, 6000, 7000, 8000],
                         requested=[1, 2, 3]))
        self.assertEqual(upd["limit"], R.MAX_READ_LINES)
        self.assertIn("first %d" % R.MAX_READ_LINES, note)

    def test_old_sidecar_without_page_index_falls_back_to_preview(self):
        upd, _ = R.text_rewrite_input({}, self.res(lines=5000, requested=[2],
                                                   page_lines=None))
        self.assertEqual(upd["limit"], R.PREVIEW_LINES)


# --- png writer -------------------------------------------------------------


def png_chunks(data):
    """[(tag, body)] for a PNG, checking every CRC on the way through.

    Written out by hand rather than with Pillow on purpose: a decoder that
    shares code with the encoder proves nothing about the file on disk.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG signature: %r" % data[:8])
    out, i = [], 8
    while i < len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        tag, body = data[i + 4:i + 8], data[i + 8:i + 8 + length]
        (crc,) = struct.unpack(">I", data[i + 8 + length:i + 12 + length])
        if crc != zlib.crc32(tag + body) & 0xFFFFFFFF:
            raise AssertionError("bad CRC on chunk %r" % tag)
        out.append((tag, body))
        i += 12 + length
    return out


def png_read(path):
    """(width, height, bit_depth, colour_type, rows) with the filter byte
    stripped from each row. Every row this writer emits is filter 0."""
    with open(path, "rb") as fh:
        chunks = png_chunks(fh.read())
    tags = [t for t, _ in chunks]
    if tags[0] != b"IHDR" or tags[-1] != b"IEND":
        raise AssertionError("chunk order: %r" % (tags,))
    w, h, depth, ctype, comp, filt, inter = struct.unpack(
        ">IIBBBBB", dict(chunks)[b"IHDR"])
    if (comp, filt, inter) != (0, 0, 0):
        raise AssertionError("unexpected IHDR flags")
    raw = zlib.decompress(b"".join(b for t, b in chunks if t == b"IDAT"))
    per_px = {0: 1, 2: 3}[ctype]
    stride = w * per_px + 1
    rows = []
    for y in range(h):
        row = raw[y * stride:(y + 1) * stride]
        if row[0] != 0:
            raise AssertionError("row %d uses filter %d" % (y, row[0]))
        rows.append(row[1:])
    return w, h, depth, ctype, rows


class _Bitmap(object):
    """The three attributes Pdfium.render() reads off a pypdfium2 bitmap."""

    def __init__(self, width, height, n_channels, pixels, pad=0,
                 rev_byteorder=False):
        self.width, self.height = width, height
        self.n_channels = n_channels
        self.stride = width * n_channels + pad
        self.rev_byteorder = rev_byteorder
        self.buffer = memoryview(bytearray(pixels))


class _Page(object):
    def __init__(self, bmp):
        self.bmp = bmp

    def render(self, scale=1.0):
        return self.bmp


class TestPngWriter(TempCache):
    """The renderer used to reach Pillow through pypdfium2's .to_pil(), which
    made a compiled image library a hard dependency of a hook whose whole
    selling point is that it works out of the box. write_png() replaces it,
    so these tests check the bytes rather than trusting a round-trip."""

    def render_fake(self, bmp, name="fake.png"):
        """Drive the real Pdfium.render() with a bitmap of our own."""
        eng = R.Pdfium.__new__(R.Pdfium)
        eng.doc = {0: _Page(bmp)}
        return R.Pdfium.render(eng, 0, 1.5, os.path.join(self.tmp, name))

    def test_a_rendered_page_is_a_well_formed_rgb_png(self):
        self.poppler(False)
        _, res = R.decide(self.blank_file(12, "png.pdf"), "2")
        w, h, depth, ctype, rows = png_read(res.imgs[0][1])
        self.assertEqual((depth, ctype), (8, 2))            # 8-bit truecolour
        # A4 at RENDER_SCALE, give or take pdfium's rounding of a half pixel
        self.assertAlmostEqual(w, 595 * R.RENDER_SCALE, delta=1)
        self.assertAlmostEqual(h, 842 * R.RENDER_SCALE, delta=1)
        self.assertEqual(len(rows), h)
        self.assertEqual(len(rows[0]), w * 3)
        self.assertTrue(R.png_is_complete(res.imgs[0][1]))
        # a blank page renders white, and white is white in any channel order
        self.assertEqual(set(rows[h // 2]), {0xFF})

    def test_the_hook_never_mentions_pillow(self):
        with open(HOOK, encoding="utf-8") as fh:
            src = fh.read()
        for word in ("PIL", "Pillow", "pillow", "to_pil"):
            self.assertNotIn(word, src, word)

    def test_rendering_works_with_pillow_unimportable(self):
        """Proof by removal: block PIL in a fresh interpreter and render."""
        path = self.blank_file(12, "nopil.pdf")
        code = (
            "import sys\n"
            "class Block(object):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in ('PIL', 'Pillow'):\n"
            "            raise ImportError(name)\n"
            "sys.meta_path.insert(0, Block())\n"
            "sys.path.insert(0, %r)\n"
            "import pdf_text_router as R\n"
            "eng = R.open_pdf(%r)\n"
            "try:\n"
            "    eng.render(0, 1.5, %r)\n"
            "finally:\n"
            "    eng.close()\n"
            "assert 'PIL' not in sys.modules\n"
            "print('ok')\n"
            % (os.path.dirname(HOOK), path, os.path.join(self.tmp, "nopil.png")))
        p = subprocess.run([sys.executable, "-c", code],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"ok", p.stdout)
        w, h, _, ctype, rows = png_read(os.path.join(self.tmp, "nopil.png"))
        self.assertEqual(ctype, 2)
        self.assertEqual(len(rows), h)

    def test_three_channels_are_bgr_and_get_swapped(self):
        """pdfium hands back BGR; a PNG is RGB. Getting this backwards is
        invisible on a greyscale scan and wrong on every colour figure."""
        px = bytes([1, 2, 3, 4, 5, 6,       # two pixels, row 0
                    7, 8, 9, 10, 11, 12])   # two pixels, row 1
        dest = self.render_fake(_Bitmap(2, 2, 3, px))
        w, h, _, ctype, rows = png_read(dest)
        self.assertEqual((w, h, ctype), (2, 2, 2))
        self.assertEqual(list(rows[0]), [3, 2, 1, 6, 5, 4])
        self.assertEqual(list(rows[1]), [9, 8, 7, 12, 11, 10])

    def test_rev_byteorder_is_already_rgb(self):
        px = bytes([1, 2, 3, 4, 5, 6])
        dest = self.render_fake(_Bitmap(2, 1, 3, px, rev_byteorder=True),
                                "rev.png")
        _, _, _, _, rows = png_read(dest)
        self.assertEqual(list(rows[0]), [1, 2, 3, 4, 5, 6])

    def test_four_channels_drop_the_alpha_and_swap(self):
        px = bytes([1, 2, 3, 255, 4, 5, 6, 128])    # BGRA, BGRA
        dest = self.render_fake(_Bitmap(2, 1, 4, px), "bgra.png")
        w, h, depth, ctype, rows = png_read(dest)
        self.assertEqual((w, h, depth, ctype), (2, 1, 8, 2))
        self.assertEqual(list(rows[0]), [3, 2, 1, 6, 5, 4])

    def test_one_channel_is_written_as_greyscale(self):
        dest = self.render_fake(_Bitmap(3, 2, 1, bytes([0, 128, 255,
                                                        7, 8, 9])), "grey.png")
        w, h, depth, ctype, rows = png_read(dest)
        self.assertEqual((w, h, depth, ctype), (3, 2, 8, 0))
        self.assertEqual(list(rows[0]), [0, 128, 255])
        self.assertEqual(list(rows[1]), [7, 8, 9])

    def test_row_padding_is_not_copied_into_the_image(self):
        """stride is not always width * channels; copying the padding shears
        the picture one row further over on every line."""
        px = bytes([1, 2, 3, 99, 99,        # one pixel + 2 bytes of padding
                    4, 5, 6, 99, 99])
        dest = self.render_fake(_Bitmap(1, 2, 3, px, pad=2), "pad.png")
        _, _, _, _, rows = png_read(dest)
        self.assertEqual(list(rows[0]), [3, 2, 1])
        self.assertEqual(list(rows[1]), [6, 5, 4])

    def test_an_unsupported_channel_count_is_refused(self):
        with self.assertRaises(ValueError):
            R.write_png(os.path.join(self.tmp, "x.png"), b"\x00\x00", 1, 1,
                        2, 2)


# --- missing dependencies ---------------------------------------------------


class TestInstallNotice(TempCache):
    """Before this, an install with no PDF engine -- or with pypdf only, met
    by a scan -- was indistinguishable from a working one: the hook allowed
    the Read and said nothing, so it looked installed and did nothing."""

    def main_output(self, path, **extra):
        """Run main() in-process (so the patches above apply) and return its
        stdout, which is the whole contract."""
        ti = dict(file_path=path, **extra)
        payload = {"tool_name": "Read", "tool_input": ti}
        saved_in, saved_out = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(json.dumps(payload))
        sys.stdout = io.StringIO()
        try:
            R.main()
        except SystemExit as exc:
            self.assertIn(exc.code, (0, None))
        finally:
            out = sys.stdout.getvalue()
            sys.stdin, sys.stdout = saved_in, saved_out
        return out

    def hook_output(self, path, **extra):
        d = self.main_output(path, **extra)
        if not d:
            return None
        d.encode("ascii")                  # must survive any console codepage
        return json.loads(d)["hookSpecificOutput"]

    def no_engines(self):
        R.available_engines = lambda: []

    def marker(self):
        return os.path.join(R.CACHE_DIR, R.INSTALL_NOTICE_NAME)

    def test_available_engines_agrees_with_the_active_one(self):
        self.assertEqual(R.active_engine_name(), R.available_engines()[0].name)
        self.assertIs(R.available_engines()[0], R.Pdfium)

    def test_no_engine_names_the_pip_install_that_fixes_it(self):
        self.no_engines()
        d = self.hook_output(self.text_file(2, "noeng.pdf"))
        self.assertEqual(d["permissionDecision"], "allow")
        self.assertNotIn("updatedInput", d)
        self.assertIn("pip install pypdfium2", d["additionalContext"])
        self.assertIn("cannot", d["additionalContext"])
        self.assertIn("proceeds", d["additionalContext"])

    def test_the_notice_is_throttled(self):
        self.no_engines()
        path = self.text_file(2, "throttle.pdf")
        self.assertIsNotNone(self.hook_output(path))
        for _ in range(3):
            self.assertEqual(self.main_output(path), "")

    def test_the_notice_comes_back_after_the_window(self):
        self.no_engines()
        path = self.text_file(2, "again.pdf")
        self.assertIsNotNone(self.hook_output(path))
        old = time.time() - (R.INSTALL_NOTICE_DAYS + 1) * 86400
        os.utime(self.marker(), (old, old))
        self.assertIsNotNone(self.hook_output(path))

    def test_a_file_that_fails_to_parse_is_never_blamed_on_a_package(self):
        """Engines are installed and the PDF is broken. Both end in a silent
        allow today; only one of them is a missing dependency."""
        path = fixtures.write(self.tmp, "broken.pdf", b"%PDF-1.4 nope")
        self.assertEqual(self.main_output(path), "")
        self.assertFalse(os.path.exists(self.marker()))

    def test_a_working_read_writes_no_marker(self):
        self.poppler(True)
        self.assertNotEqual(self.main_output(self.text_file(2, "fine.pdf")), "")
        self.assertFalse(os.path.exists(self.marker()))

    def test_extract_only_engine_meeting_a_scan_asks_for_pypdfium2(self):
        R.ENGINES = (R.PyPdf,)
        self.poppler(False)
        path = self.blank_file(12, "scan-pypdf.pdf")
        d = self.hook_output(path)
        self.assertEqual(d["permissionDecision"], "allow")
        self.assertNotIn("updatedInput", d)
        self.assertIn("pip install pypdfium2", d["additionalContext"])
        self.assertIn("pypdf", d["additionalContext"])
        self.assertIn("cannot render", d["additionalContext"])
        self.assertEqual(self.main_output(path), "")        # throttled too

    def test_extract_only_engine_on_a_text_file_says_nothing(self):
        """pypdf can do this job perfectly well; nothing to report."""
        R.ENGINES = (R.PyPdf,)
        self.poppler(True)
        d = self.hook_output(self.text_file(3, "pypdf-text.pdf"))
        self.assertIn("updatedInput", d)
        self.assertFalse(os.path.exists(self.marker()))

    def test_an_unwritable_cache_dir_is_silent_not_fatal(self):
        """No writable directory means no way to throttle, and a note on
        every single Read would be worse than none."""
        blocker = fixtures.write(self.tmp, "not-a-dir", b"x")
        R.CACHE_DIR = os.path.join(blocker, "cache")
        self.no_engines()
        self.assertFalse(R.install_notice_due())
        self.assertEqual(self.main_output(self.text_file(2, "ro.pdf")), "")

    def test_selftest_reports_which_of_the_three_states_this_is(self):
        p = subprocess.run([sys.executable, HOOK, "--selftest"],
                           stdout=subprocess.PIPE)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"status           full", p.stdout)
        self.assertNotIn(b"Pillow", p.stdout)

    def _selftest_without(self, *modules):
        code = (
            "import sys\n"
            "class Block(object):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in %r:\n"
            "            raise ImportError(name)\n"
            "sys.meta_path.insert(0, Block())\n"
            "sys.path.insert(0, %r)\n"
            "import pdf_text_router as R\n"
            "sys.exit(R.selftest())\n" % (modules, os.path.dirname(HOOK)))
        return subprocess.run([sys.executable, "-c", code],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_selftest_reports_the_extract_only_state(self):
        p = self._selftest_without("pypdfium2", "fitz")
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn(b"status           EXTRACT ONLY", p.stdout)
        self.assertIn(b"pip install pypdfium2", p.stdout)

    def test_selftest_reports_the_no_engine_state(self):
        p = self._selftest_without("pypdfium2", "fitz", "pypdf")
        self.assertEqual(p.returncode, 1)
        self.assertIn(b"status           NONE", p.stdout)
        self.assertIn(b"pip install pypdfium2", p.stdout)


# --- preview budget ---------------------------------------------------------


class TestPreviewLines(unittest.TestCase):
    """PREVIEW_LINES used to be a flat 200, which is a different amount of
    context per file by a factor of five or more."""

    def test_dense_text_gets_fewer_lines_than_sparse_text(self):
        dense = R.preview_lines(text_tokens=30000, lines=1000)   # 30 tok/line
        sparse = R.preview_lines(text_tokens=10000, lines=1000)  # 10 tok/line
        self.assertLess(dense, sparse)
        self.assertEqual(dense, R.PREVIEW_TOKENS // 30)
        self.assertEqual(sparse, R.PREVIEW_TOKENS // 10)

    def test_a_cjk_file_shows_fewer_lines_than_a_latin_one(self):
        """Same line count, same page count, different scripts: the Chinese
        file is worth several times the tokens per line."""
        latin = "The quick brown fox jumps over the lazy dog. " * 2
        cjk = CJK * 2
        n = 900
        latin_lines = R.preview_lines(R.est_text_tokens(latin) * n, n)
        cjk_lines = R.preview_lines(R.est_text_tokens(cjk) * n, n)
        self.assertLess(cjk_lines, latin_lines)

    def test_the_ceiling_holds_for_a_nearly_empty_file(self):
        self.assertEqual(R.preview_lines(10, 100000), R.PREVIEW_LINES)
        self.assertEqual(R.preview_lines(0, 5000), R.PREVIEW_LINES)

    def test_the_floor_holds_for_a_very_dense_file(self):
        self.assertEqual(R.preview_lines(500000, 100), R.PREVIEW_LINES_MIN)

    def test_no_division_by_zero_on_an_empty_file(self):
        self.assertEqual(R.preview_lines(0, 0), R.PREVIEW_LINES)
        self.assertEqual(R.preview_lines(0, None), R.PREVIEW_LINES)


# --- process contract -------------------------------------------------------


class TestProcessContract(TempCache):
    """The hook is a process: what matters is stdout and the exit code."""

    def run_hook(self, payload, raw=None, mode=None):
        env = dict(os.environ, PDF_TEXT_ROUTER_CACHE=R.CACHE_DIR)
        env.pop("PDF_TEXT_ROUTER_MODE", None)
        if mode:
            env["PDF_TEXT_ROUTER_MODE"] = mode
        data = raw if raw is not None else json.dumps(payload).encode()
        p = subprocess.run([sys.executable, HOOK], input=data,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip(), p.stderr

    def read(self, path, **extra):
        ti = dict(file_path=path, **extra)
        return {"tool_name": "Read", "tool_input": ti}

    def test_allow_is_silence_for_a_real_pdf(self):
        """Not a .py file -- the extension check would short-circuit that and
        main() would never be exercised on a PDF at all."""
        path = self.blank_file(3, "silent.pdf")
        rc, out, err = self.run_hook(self.read(path))
        self.assertEqual((rc, out, err), (0, "", b""))

    def test_other_tools_pass_through(self):
        path = self.text_file(2)
        rc, out, _ = self.run_hook({"tool_name": "Write",
                                    "tool_input": {"file_path": path}})
        self.assertEqual((rc, out), (0, ""))

    def test_missing_tool_name_passes_through(self):
        """Only a Read is ours to redirect; no name is not a Read."""
        path = self.text_file(2, "noname.pdf")
        rc, out, err = self.run_hook({"tool_input": {"file_path": path}})
        self.assertEqual((rc, out, err), (0, "", b""))

    def test_missing_file_passes_through(self):
        rc, out, _ = self.run_hook(self.read("/nope/x.pdf"))
        self.assertEqual((rc, out), (0, ""))

    def test_hostile_payloads_never_break_the_contract(self):
        for raw in (b"", b"not json", b"[1,2,3]", b"null", b'"str"',
                    json.dumps({"tool_input": []}).encode(),
                    json.dumps({"tool_name": 7}).encode(),
                    json.dumps({"tool_name": "Read",
                                "tool_input": {"file_path": 5}}).encode()):
            rc, out, err = self.run_hook(None, raw=raw)
            self.assertEqual(rc, 0, raw)
            self.assertEqual(err, b"", raw)
            if out:
                json.loads(out)

    def test_default_mode_rewrites_the_read_to_the_text(self):
        path = self.text_file(3, "rw.pdf")
        rc, out, _ = self.run_hook(self.read(path))
        self.assertEqual(rc, 0)
        out.encode("ascii")
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(d["hookEventName"], "PreToolUse")
        self.assertEqual(d["permissionDecision"], "allow")
        self.assertTrue(d["updatedInput"]["file_path"].endswith(".txt"))
        self.assertTrue(os.path.isfile(d["updatedInput"]["file_path"]))
        self.assertNotIn("limit", d["updatedInput"])
        self.assertIn("redirected", d["additionalContext"])
        self.assertIn("rw.pdf", d["additionalContext"])
        self.assertIn("[page N/3]", d["additionalContext"])

    def test_deny_mode_keeps_the_old_contract(self):
        path = self.text_file(3, "deny.pdf")
        rc, out, _ = self.run_hook(self.read(path), mode="deny")
        self.assertEqual(rc, 0)
        out.encode("ascii")
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(d["permissionDecision"], "deny")
        self.assertNotIn("updatedInput", d)
        self.assertIn("ACTION:", d["permissionDecisionReason"])
        self.assertIn(".txt", d["permissionDecisionReason"])

    def test_unknown_mode_is_rewrite(self):
        path = self.text_file(2, "mode.pdf")
        _, out, _ = self.run_hook(self.read(path), mode="whatever")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"],
                         "allow")

    def test_large_text_gets_a_preview_limit(self):
        path = self.text_file(70, "big.pdf", lines_per_page=30)   # > 1800 lines
        _, out, _ = self.run_hook(self.read(path))
        d = json.loads(out)["hookSpecificOutput"]
        limit = d["updatedInput"]["limit"]
        self.assertTrue(R.PREVIEW_LINES_MIN <= limit <= R.PREVIEW_LINES, limit)
        self.assertIn("first %d lines" % limit, d["additionalContext"])
        self.assertIn("Grep", d["additionalContext"])
        # and the window really is worth about PREVIEW_TOKENS
        with open(d["updatedInput"]["file_path"], encoding="utf-8") as fh:
            head = "".join(fh.readlines()[:limit])
        self.assertLess(abs(R.est_text_tokens(head) - R.PREVIEW_TOKENS),
                        R.PREVIEW_TOKENS // 2)

    def test_callers_offset_is_not_overridden(self):
        path = self.text_file(70, "big2.pdf", lines_per_page=30)
        _, out, _ = self.run_hook(self.read(path, offset=500))
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(set(d["updatedInput"]), {"file_path"})

    def test_page_range_on_text_positions_the_read(self):
        path = self.text_file(70, "big3.pdf", lines_per_page=30)
        _, out, _ = self.run_hook(self.read(path, pages="20-25"))
        d = json.loads(out)["hookSpecificOutput"]
        upd = d["updatedInput"]
        self.assertGreater(upd["offset"], 19 * 30)
        self.assertLessEqual(upd["limit"], R.MAX_READ_LINES)
        with open(upd["file_path"], encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        window = "\n".join(lines[upd["offset"] - 1: upd["offset"] - 1 + upd["limit"]])
        self.assertIn("[page 20/70]", window)
        self.assertIn("[page 25/70]", window)
        self.assertNotIn("[page 19/70]", window)
        self.assertNotIn("[page 26/70]", window)

    def test_blank_pages_reach_the_model(self):
        path = self.text_file(20, "gap.pdf", blank=[7, 12])
        _, out, _ = self.run_hook(self.read(path))
        d = json.loads(out)["hookSpecificOutput"]
        self.assertIn("Pages 7, 12 of 20 are blank (no text, no image); skip them.",
                      d["additionalContext"])

    def test_page_classes_reach_the_model_apart(self):
        path = self.mixed_file("classes.pdf")
        for mode in ("rewrite", "deny"):
            _, out, _ = self.run_hook(self.read(path), mode=mode)
            d = json.loads(out)["hookSpecificOutput"]
            text = d.get("additionalContext") or d["permissionDecisionReason"]
            self.assertIn("Page 2 of 4 is blank (no text, no image); skip it.", text)
            self.assertIn('Page 3 of 4 has no readable text layer (marked '
                          '"(image page, ...)"', text)
            self.assertIn('pages="3" to see one', text)
            self.assertIn("Page 4 of 4 is mostly picture with little text", text)
            self.assertIn('pages="4" to see it', text)

    def test_check_prints_the_three_classes(self):
        path = self.mixed_file("check-classes.pdf")
        env = dict(os.environ, PDF_TEXT_ROUTER_CACHE=R.CACHE_DIR)
        p = subprocess.run([sys.executable, HOOK, "--check", path],
                           stdout=subprocess.PIPE, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"blank pages    2", p.stdout)
        self.assertIn(b"image pages    3", p.stdout)
        self.assertIn(b"figure pages   4", p.stdout)

    def test_render_rewrite_points_at_the_first_png(self):
        if R.has_poppler():
            self.skipTest("poppler present: the native path is allowed instead")
        path = self.blank_file(12, "scan.pdf")
        _, out, _ = self.run_hook(self.read(path))
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(d["permissionDecision"], "allow")
        first = d["updatedInput"]["file_path"]
        self.assertTrue(first.endswith(".png"))
        self.assertTrue(R.png_is_complete(first))
        self.assertIn("page 10 ->", d["additionalContext"])
        self.assertIn("2 more page(s)", d["additionalContext"])

    def test_render_deny_lists_every_png(self):
        if R.has_poppler():
            self.skipTest("poppler present: the native path is allowed instead")
        path = self.blank_file(12, "scan2.pdf")
        _, out, _ = self.run_hook(self.read(path, pages="2,5"), mode="deny")
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(d["permissionDecision"], "deny")
        self.assertIn("-p2-", d["permissionDecisionReason"])
        self.assertIn("-p5-", d["permissionDecisionReason"])

    def test_bad_pages_deny_in_both_modes(self):
        path = self.text_file(3, "bp.pdf")
        for mode in ("rewrite", "deny"):
            _, out, _ = self.run_hook(self.read(path, pages="2-"), mode=mode)
            d = json.loads(out)["hookSpecificOutput"]
            self.assertEqual(d["permissionDecision"], "deny", mode)
            self.assertIn("comma list", d["permissionDecisionReason"])
            _, out, _ = self.run_hook(self.read(path, pages="7"), mode=mode)
            d = json.loads(out)["hookSpecificOutput"]
            self.assertEqual(d["permissionDecision"], "deny", mode)
            self.assertIn("page 7 does not exist", d["permissionDecisionReason"])

    def test_garbage_pdf_logs_and_allows(self):
        path = fixtures.write(self.tmp, "junk.pdf", b"%PDF-1.4 nope")
        rc, out, err = self.run_hook(self.read(path))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn(b"pdf-text-router:", err)

    def test_selftest_exits_clean(self):
        p = subprocess.run([sys.executable, HOOK, "--selftest"],
                           stdout=subprocess.PIPE)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"active engine", p.stdout)
        self.assertIn(b"pdfinfo", p.stdout)
        self.assertIn(b"mode", p.stdout)
        self.assertIn(b"cache usage", p.stdout)

    def test_check_reports_without_writing(self):
        path = self.text_file(4, "check.pdf")
        env = dict(os.environ, PDF_TEXT_ROUTER_CACHE=R.CACHE_DIR)
        p = subprocess.run([sys.executable, HOOK, "--check", path],
                           stdout=subprocess.PIPE, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"decision", p.stdout)
        self.assertIn(b"rewrite", p.stdout)
        self.assertFalse(os.path.isdir(R.CACHE_DIR))


if __name__ == "__main__":
    unittest.main(verbosity=2)
