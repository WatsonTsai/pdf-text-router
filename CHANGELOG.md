# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

## [0.3.0] - 2026-08-29

### Changed
- Render PNGs with zlib + struct instead of Pillow, so pypdfium2 is the only
  package the hook needs; output is pixel-identical, about 22% faster and
  about 42% larger on disk (fixed filter 0 per row against Pillow's adaptive
  choice).
- The preview of a large extraction is now a 3,000-token budget rather than
  a fixed 200 lines, with the line count derived from the file's own
  measured density (203-400 lines across the corpus).
- CI no longer installs pillow, so a green run is now evidence that the
  pillow-free path works.

### Fixed
- A missing engine used to fail open in silence, leaving the hook installed
  and inert. The allowed Read now carries a note naming the pip install
  that fixes it, at most once a week; a parse failure still says nothing.

### Added
- `--selftest` reports which of the three engine states it is in.
- Test suite grew to 189 tests (was 163).

## [0.2.0] - 2026-08-27

### Added
- PreToolUse now allows the Read with `updatedInput` pointing at the
  extracted `.txt` (or first rendered PNG) plus `additionalContext`;
  `PDF_TEXT_ROUTER_MODE=deny` keeps the old contract for Claude Code
  versions without `updatedInput`.
- Page selection parsed as `N`, `N-M`, or comma lists; bad or out-of-range
  ranges deny with the page count.
- Every page classed text / blank / image / figure from character count and
  image-object area; scans decided per page, not by document average.
- Cache keyed on size+mtime+head/tail hash with a JSON sidecar, so a hit
  never opens the PDF; LRU prune at 500 MB / 30 days; `--clear-cache`;
  `PDF_TEXT_ROUTER_CACHE=beside` keeps files next to the PDF.
- Render up to 20 pages of an explicit range, first 10 of a long scan;
  `PDF_TEXT_ROUTER_SCALE`.
- Benchmark groups by manifest category and by native path; results checked
  in.
- CI on three OSes (`.github/workflows/test.yml`).
- Test suite grew to 163 tests (was 59), including corpus regression when
  `corpus/` is present.

### Changed
- Engines fall back on parse failure; pdfium reports password protection
  via `FPDF_ERR_PASSWORD`; failures logged to stderr, never blocking.
- Image tokens estimated with the documented `ceil(w/28)*ceil(h/28)`
  formula.
- README rewritten: symptom first, "is this for you", official PDF skill
  comparison, uninstall, cache privacy.

## [0.1.0] - 2026-08-24

### Added
- Initial release: a PreToolUse hook that routes each Read on a PDF to
  whichever path works — text layer to UTF-8 extraction, scans to vision,
  and scans without poppler to pages rendered locally rather than to an
  error. Out-of-range page numbers are reported instead of being waved
  through to a failure that mentions poppler and not the real problem.
  Everything unexpected falls open.
- Defaults to pypdfium2 (BSD-3/Apache-2.0) rather than PyMuPDF, whose AGPL
  licence is a problem for the people most interested in token cost;
  measured equal on CJK extraction, where pypdf drops 7.7% of the
  characters.
- 35-file public benchmark corpus (ODC-BY, CC-BY, US/JP government terms)
  with checksums, so the README's numbers are reproducible — including the
  case against the tool: on the LoC government-document set the pooled
  ratio is 0.96x, a net loss.
- 59 tests, fixtures built in memory. Verified by mutation: ten deliberate
  regressions, nine caught, the tenth a redundant guard.
