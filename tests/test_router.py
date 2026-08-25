"""Tests for pdf-text-router.

Run: python -m unittest discover -s tests

Some of these exist because an earlier version of this suite passed while the
thing it claimed to test was broken. Where that happened, the test says so.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import pdf_text_router as R          # noqa: E402
import fixtures                      # noqa: E402

HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks", "pdf_text_router.py")

CJK = "研究計畫：氮素利用效率與生物性硝化抑制。第一節、材料與方法。"


class TempCache(unittest.TestCase):
    """Every test writes into a throwaway cache dir, never the user's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ptr-test-")
        self._cache = R.CACHE_DIR
        R.CACHE_DIR = os.path.join(self.tmp, "cache")
        self._poppler = R.has_poppler
        self._engines = R.ENGINES

    def tearDown(self):
        R.CACHE_DIR = self._cache
        R.has_poppler = self._poppler
        R.ENGINES = self._engines
        shutil.rmtree(self.tmp, ignore_errors=True)

    def poppler(self, present):
        R.has_poppler = lambda: present

    def text_file(self, pages=2, name=None):
        return fixtures.write(self.tmp, name or "t%d.pdf" % pages,
                              fixtures.text_pdf(pages))

    def blank_file(self, pages=2, name=None):
        return fixtures.write(self.tmp, name or "b%d.pdf" % pages,
                              fixtures.blank_pdf(pages))


class TestPureHelpers(unittest.TestCase):

    def test_span_parses_ranges(self):
        self.assertEqual(R.span_of("1-5"), 5)
        self.assertEqual(R.span_of("3"), 1)
        self.assertEqual(R.span_of(" 2 - 4 "), 3)

    def test_span_rejects_junk(self):
        for bad in ("abc", "", "1-", "-3", None, 5, "1,2", {}, []):
            self.assertIsNone(R.span_of(bad), bad)

    def test_span_never_negative(self):
        self.assertEqual(R.span_of("9-2"), 1)

    def test_first_page(self):
        self.assertEqual(R.first_page_of("7-9"), 7)
        self.assertEqual(R.first_page_of("junk"), 1)
        self.assertEqual(R.first_page_of(None), 1)

    def test_cjk_counts_as_denser_tokens(self):
        self.assertAlmostEqual(R.est_text_tokens("abcd" * 100), 100, delta=2)
        self.assertAlmostEqual(R.est_text_tokens("中" * 100), 76, delta=2)

    def test_cjk_ratio_beats_a_single_divisor(self):
        """The bug this replaces: chars//3 undercounts Chinese 2-3x."""
        cjk = "中文" * 500
        self.assertGreater(R.est_text_tokens(cjk), (len(cjk) // 3) * 1.9)

    def test_image_tokens_scale_with_pages(self):
        one = R.est_image_tokens(595, 842, 1, dpi=100)
        self.assertAlmostEqual(R.est_image_tokens(595, 842, 10, dpi=100),
                               one * 10, delta=10)

    def test_image_tokens_round_once_not_per_page(self):
        """Rounding each page separately loses about 1% over a long document."""
        self.assertGreater(R.est_image_tokens(595, 842, 1000, dpi=100),
                           int(R.est_image_tokens(595, 842, 1, dpi=100)) * 1000)

    def test_image_tokens_respect_the_long_edge_cap(self):
        uncapped = R.est_image_tokens(5000, 5000, 1)
        capped = R.est_image_tokens(5000, 5000, 1, cap=1568)
        self.assertLess(capped, uncapped)
        self.assertAlmostEqual(capped, 1568 * 1568 / 750.0, delta=2)

    def test_image_tokens_never_negative(self):
        self.assertGreaterEqual(R.est_image_tokens(-595, 842, 3, dpi=100), 0)
        self.assertEqual(R.est_image_tokens(595, 842, -4, dpi=100), 0)

    def test_clean_normalises_newlines_and_strips_controls(self):
        out = R.clean("a\r\nb\r c\x00d\te")
        self.assertNotIn("\r", out)
        self.assertNotIn("\x00", out)
        self.assertIn("\t", out)

    def test_clean_keeps_cjk(self):
        self.assertEqual(R.clean(CJK), CJK)


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

    def test_deny_reason_is_ascii_even_for_a_cjk_path(self):
        payload = R.text_message(n=3, total=10, text_tokens=5, img_tokens=9,
                                 path_note="poppler path", native_works=False,
                                 dest="C:/暫存/研究.txt", lines=4)
        json.dumps({"r": payload}, ensure_ascii=True).encode("ascii")


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
        with open(a, "rb") as fh:
            data = bytearray(fh.read())
        data[-20:-17] = b"ZZZ"
        with open(a, "wb") as fh:
            fh.write(bytes(data))
        self.assertEqual(len(bytes(data)), os.path.getsize(a))
        self.assertNotEqual(k1, R.cache_key(a))

    def test_key_is_stable_for_an_untouched_file(self):
        a = self.text_file(1, "stable.pdf")
        self.assertEqual(R.cache_key(a), R.cache_key(a))

    def test_large_file_keying_samples_both_ends(self):
        head = b"%PDF-1.4\n" + b"A" * (R.SAMPLE_BYTES * 3)
        p1 = fixtures.write(self.tmp, "big1.pdf", head + b"TAIL-ONE")
        p2 = fixtures.write(self.tmp, "big2.pdf", head + b"TAIL-TWO")
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
        with open(payload[7], encoding="utf-8") as fh:
            self.assertIn("===== [page 1/3]", fh.read())

    def test_empty_text_cache_is_rejected(self):
        path = self.text_file(3, "empty-cache.pdf")
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        open(R.text_cache_path(path), "w").close()
        self.poppler(True)
        _, payload = R.decide(path, None)
        self.assertGreater(payload[8], 3)

    def test_zero_byte_png_is_rerendered(self):
        path = self.blank_file(12, "zero-png.pdf")
        os.makedirs(R.CACHE_DIR, exist_ok=True)
        stale = R.image_cache_path(path, 2, R.RENDER_SCALE)
        open(stale, "w").close()
        self.poppler(False)
        _, payload = R.decide(path, None)
        page2 = dict(payload[4])[2]
        self.assertGreater(os.path.getsize(page2), 0)
        self.assertTrue(R.png_is_complete(page2))

    def test_no_temp_files_are_left_behind(self):
        self.poppler(False)
        R.decide(self.blank_file(12, "clean.pdf"), None)
        R.decide(self.text_file(4, "clean2.pdf"), None)
        leftovers = [f for f in os.listdir(R.CACHE_DIR) if ".tmp" in f]
        self.assertEqual(leftovers, [])

    def test_complete_cache_is_reused_not_rewritten(self):
        self.poppler(True)
        path = self.text_file(3, "reuse.pdf")
        _, first = R.decide(path, None)
        stamp = os.path.getmtime(first[7])
        _, second = R.decide(path, None)
        self.assertEqual(first[7], second[7])
        self.assertEqual(stamp, os.path.getmtime(second[7]))


class TestEngines(TempCache):
    """Fallback happens when a package is missing, not when one file fails."""

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

    def test_pdfium_releases_the_file_handle(self):
        """Windows refuses to delete a PDF that pdfium still holds open."""
        path = self.text_file(1, "handle.pdf")
        self.poppler(True)
        R.decide(path, None)
        os.remove(path)
        self.assertFalse(os.path.exists(path))


class TestRouting(TempCache):

    def test_text_layer_whole_file_extracts(self):
        self.poppler(True)
        action, payload = R.decide(self.text_file(2), None)
        self.assertEqual(action, "text")
        self.assertTrue(os.path.isfile(payload[7]))

    def test_extracted_text_carries_page_markers(self):
        self.poppler(True)
        _, payload = R.decide(self.text_file(3), None)
        with open(payload[7], encoding="utf-8") as fh:
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

    def test_long_scan_renders_locally_without_poppler(self):
        self.poppler(False)
        action, payload = R.decide(self.blank_file(12), None)
        self.assertEqual(action, "render")
        self.assertEqual(len(payload[4]), 5)      # not MAX_RENDER_PAGES: that
        for _, path in payload[4]:                # would assert against itself
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(R.png_is_complete(path))

    def test_short_range_allowed_when_poppler_present(self):
        self.poppler(True)
        self.assertEqual(R.decide(self.text_file(6), "2-3")[0], "allow")

    def test_short_range_renders_locally_without_poppler(self):
        """The old hook told Claude to retry down the one path that cannot work."""
        self.poppler(False)
        action, payload = R.decide(self.text_file(6), "2-3")
        self.assertEqual(action, "render")
        self.assertEqual([p for p, _ in payload[4]], [2, 3])

    def test_wide_range_is_a_read_not_a_look(self):
        self.poppler(True)
        self.assertEqual(R.decide(self.text_file(20), "1-15")[0], "text")

    def test_absurd_range_does_not_produce_absurd_estimates(self):
        self.poppler(True)
        _, payload = R.decide(self.text_file(3), "1-999999")
        self.assertLess(payload[3], 100000)

    def test_out_of_range_is_reported_not_waved_through(self):
        """Waving it through sends Claude at the one path missing poppler."""
        self.poppler(False)
        for bad in ("0", "9-2", "99-100", "4-6"):
            action, payload = R.decide(self.blank_file(3), bad)
            self.assertEqual(action, "outofrange", bad)
            self.assertEqual(payload[1], 3)

    def test_render_never_walks_past_the_last_page(self):
        self.poppler(False)
        _, payload = R.decide(self.blank_file(12), "11-13")
        self.assertEqual([p for p, _ in payload[4]], [11, 12])

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

    def test_render_message_names_every_file(self):
        msg = R.render_message(2, 2, 9, True, [(2, "a.png"), (3, "b.png")])
        self.assertIn("a.png", msg)
        self.assertIn("b.png", msg)
        self.assertIn("pdftoppm", msg)


class TestProcessContract(TempCache):
    """The hook is a process: what matters is stdout and the exit code."""

    def run_hook(self, payload, raw=None):
        env = dict(os.environ, PDF_TEXT_ROUTER_CACHE=R.CACHE_DIR)
        data = raw if raw is not None else json.dumps(payload).encode()
        p = subprocess.run([sys.executable, HOOK], input=data,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip(), p.stderr

    def test_allow_is_silence_for_a_real_pdf(self):
        """Not a .py file -- the extension check would short-circuit that and
        main() would never be exercised on a PDF at all."""
        path = self.blank_file(3, "silent.pdf")
        rc, out, err = self.run_hook({"tool_name": "Read",
                                      "tool_input": {"file_path": path}})
        self.assertEqual((rc, out, err), (0, "", b""))

    def test_other_tools_pass_through(self):
        path = self.text_file(2)
        rc, out, _ = self.run_hook({"tool_name": "Write",
                                    "tool_input": {"file_path": path}})
        self.assertEqual((rc, out), (0, ""))

    def test_missing_file_passes_through(self):
        rc, out, _ = self.run_hook({"tool_name": "Read",
                                    "tool_input": {"file_path": "/nope/x.pdf"}})
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

    def test_deny_payload_is_pure_ascii_and_complete(self):
        path = self.text_file(3, "ascii.pdf")
        rc, out, _ = self.run_hook({"tool_name": "Read",
                                    "tool_input": {"file_path": path}})
        self.assertEqual(rc, 0)
        out.encode("ascii")
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(d["permissionDecision"], "deny")
        self.assertEqual(d["hookEventName"], "PreToolUse")
        self.assertIn("ACTION:", d["permissionDecisionReason"])

    def test_selftest_exits_clean(self):
        p = subprocess.run([sys.executable, HOOK, "--selftest"],
                           stdout=subprocess.PIPE)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"active engine", p.stdout)

    def test_check_reports_without_writing(self):
        path = self.text_file(4, "check.pdf")
        p = subprocess.run([sys.executable, HOOK, "--check", path],
                           stdout=subprocess.PIPE)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"decision", p.stdout)
        self.assertFalse(os.path.isdir(R.CACHE_DIR))


if __name__ == "__main__":
    unittest.main(verbosity=2)
