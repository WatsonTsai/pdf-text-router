"""Classification checks against the public benchmark corpus.

The 35 PDFs are not redistributed (see scripts/corpus.json for URLs and
checksums); run scripts/fetch_corpus.py to download them into corpus/.
Without that directory this whole module is skipped.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hooks"))
import pdf_text_router as R          # noqa: E402

CORPUS = os.path.join(os.path.dirname(__file__), "..", "corpus")

# Hand-verified scans: no usable text layer, or one made of private-use
# glyphs (the olmocr table), which clean() strips before counting.
SCAN_PREFIXES = (
    "loc__FBTKGVAADTR36EKHKLGBXAUK5DI2LT5Z",
    "olmocr__long_tiny_text__10a_pg1",
    "olmocr__long_tiny_text__14a_pg1",
    "olmocr__old_scans__1",
    "olmocr__old_scans__2",
    "olmocr__old_scans__3",
    "olmocr__old_scans_math__1_pg10",
    "olmocr__tables__0486cc2ec11e21b6a8280ca71073ca",
)
EXPECTED_TOTAL = 35


def classify_file(path):
    eng = R.open_pdf(path)
    if eng is None:
        raise AssertionError("no engine could open %s" % path)
    try:
        if eng.encrypted:
            raise AssertionError("%s is encrypted" % path)
        meta, _ = R.inspect_pdf(eng)
    finally:
        eng.close()
    return R.classify(meta)


# Hand-verified page classes (1-based page -> kind), measured with both
# pdfium and MuPDF: zenodo 22070313 p9 has 1 char under a 70% image (words
# burned into a picture), p8 has 86 chars beside a 45% image (a figure with
# its caption); the ndc projection's p38 and p102 have neither text nor
# image; the olmocr old scan is a single full-page image; the olmocr table
# has a text layer of private-use glyphs, which clean() strips -- an image
# page (text layer unreadable), never a blank one.
KNOWN_PAGES = {
    "zenodo__22070313": {9: "image", 8: "figure"},
    "ndc__population-projection-2024": {38: "blank", 102: "blank"},
    "olmocr__old_scans__1": {1: "image"},
    "olmocr__tables__0486cc2ec11e21b6a8280ca71073ca": {1: "image"},
}


@unittest.skipUnless(os.path.isdir(CORPUS), "corpus/ not downloaded")
class TestCorpus(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.files = sorted(f for f in os.listdir(CORPUS)
                           if f.lower().endswith(".pdf"))
        cls.verdicts = {}
        for name in cls.files:
            kinds, scanned = classify_file(os.path.join(CORPUS, name))
            cls.verdicts[name] = (kinds, scanned)

    def one(self, prefix):
        hits = [f for f in self.files if f.startswith(prefix)]
        self.assertEqual(len(hits), 1, prefix)
        return hits[0]

    def test_corpus_is_complete(self):
        self.assertEqual(len(self.files), EXPECTED_TOTAL, self.files)

    def test_known_scans_are_scans(self):
        for prefix in SCAN_PREFIXES:
            name = self.one(prefix)
            kinds, scanned = self.verdicts[name]
            self.assertTrue(scanned, "%s should be a scan (kinds=%s)"
                            % (name, kinds))

    def test_everything_else_is_text(self):
        wrong = []
        for name in self.files:
            if name.startswith(SCAN_PREFIXES):
                continue
            kinds, scanned = self.verdicts[name]
            if scanned:
                wrong.append((name, R.pages_of(kinds, "blank"),
                              R.pages_of(kinds, "image")))
        self.assertEqual(wrong, [])
        self.assertEqual(len(self.files) - len(SCAN_PREFIXES), 27)

    def test_ndc_projection_names_its_non_text_pages(self):
        kinds, scanned = self.verdicts[self.one("ndc__population-projection-2024")]
        self.assertFalse(scanned)
        self.assertNotEqual(kinds[38 - 1], "text")
        self.assertNotEqual(kinds[76 - 1], "text")

    def test_known_pages_get_their_class(self):
        for prefix, pages in KNOWN_PAGES.items():
            kinds, _ = self.verdicts[self.one(prefix)]
            for page, want in pages.items():
                self.assertEqual(kinds[page - 1], want,
                                 "%s p%d" % (prefix, page))

    def test_the_vector_map_is_an_accepted_miss(self):
        """zenodo 22072701 p28 is a map drawn with paths: 41 chars, 2% image.
        Nothing here catches it, by design -- path counts are not a signal
        worth guessing from. If this ever flips, the tunables changed."""
        kinds, _ = self.verdicts[self.one("zenodo__22072701")]
        self.assertEqual(kinds[28 - 1], "text")

    def test_decide_agrees_with_classify(self):
        """Dry-run routing on the real files, poppler absent."""
        real = R.has_poppler
        R.has_poppler = lambda: False
        try:
            for name in self.files:
                action, _ = R.decide(os.path.join(CORPUS, name), None,
                                     dry_run=True)
                _, scanned = self.verdicts[name]
                if scanned:
                    self.assertIn(action, ("allow", "render"), name)
                else:
                    self.assertEqual(action, "text", name)
        finally:
            R.has_poppler = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
