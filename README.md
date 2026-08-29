# pdf-text-router

A Claude Code hook that routes each `Read` on a PDF to the path that actually works.

English · [繁體中文](README.zh-TW.md)

You ask Claude Code to read a PDF. Nothing errors. Claude quietly switches to some other method — a `Bash` one-liner, a guess from the filename — or tells you the file could not be opened. What actually happened underneath is `pdftoppm is not installed`, and you never saw it, because Read errors are not surfaced in the TUI ([#23699](https://github.com/anthropics/claude-code/issues/23699), closed as stale). Any PDF over ten pages, and any Read with a page range, goes through poppler's `pdftoppm`, and the official setup docs have never said so ([#23704](https://github.com/anthropics/claude-code/issues/23704), open). Even when poppler *is* there, a PDF with a real text layer is being read as pictures of words.

**What you get instead.** For a PDF with a text layer, the hook rewrites the Read in place: one `Read`, and what comes back is a UTF-8 `.txt` with `[page N/M]` markers, so Claude can still cite pages, `Grep` the file, and never runs an OCR pass over text that was already text. No poppler involved. For a scan, the hook lets the native path through when it works, and when it would fail for want of poppler it renders the pages locally, points the same Read at the first PNG and lists the rest.

**What it costs.** The extracted text is cached in plain text under `~/.claude/pdf-text-cache`. The PDF was already a readable file on the same machine, so the cache adds exposure in one situation only: the PDF lives on an encrypted volume, an external drive or a network share, and the cache lands in your home directory. For that case, `PDF_TEXT_ROUTER_CACHE=beside` keeps the cache next to the PDF, or point the variable at a directory of your own choosing; there is no encrypted mode (see [Cache](#design-notes)). Slide decks are the biggest saving and also the biggest trap: text drawn inside a figure — station names on a map, a scale bar, a whole slide exported as an image — is not in the text layer. The hook classes every page as text, blank, image or figure from its character count and the share of it covered by image objects, so it can flag a slide that is one big picture and a page that is a large figure with a caption; what it cannot flag is a vector drawing — a map or a chart drawn as paths has an image area near zero and reads as a sparse text page. The first Read of a large file pays for the extraction (the 1,039-page bill in the corpus takes 2.1–2.3 s with `pypdfium2`); every later Read is a cache hit that never opens the PDF (0.3 s on the same file).

## Is this for you?

Two questions.

1. `which pdftoppm` (Unix) or `where pdftoppm` (Windows). If it prints nothing, Claude Code cannot currently read any PDF over ten pages, or any page range, and this hook is the difference between a document and an error message.
2. `python scripts/benchmark.py <a folder of your own PDFs>`. It prints, for each file, what Claude Code would spend on images versus what the text costs, and a median.

Reading the result:

- Median ratio above 1.5× — install it; the token saving alone pays for the hook.
- Your pages carry more than ~5,000 Latin characters (or ~1,600 CJK characters) each — on the poppler path the text layer costs *more* than the images. What you still get is `grep`-able, lossless text, and a document that opens at all when poppler is absent. Decide on that, not on tokens.

Not for you if you already pipe PDFs through `pdftotext` or an MCP server that pre-processes documents — you have solved the same problem one layer up. (The official PDF skill is a different case; see the next section.) Also not for you if a plain-text cache of everything you read is unacceptable; there is no encrypted mode.

## Compared with the official PDF skill

Anthropic's [`anthropics/skills` PDF skill](https://github.com/anthropics/skills/tree/main/skills/pdf) is loaded when the model decides a request is about PDF work, and then runs Bash scripts (pdfplumber, pypdf, reportlab, qpdf). It does not change what the `Read` tool does. It covers text and table extraction, merging, splitting, rotating, form filling, watermarks, encryption, creating new PDFs, and OCR through pytesseract + pdf2image, which needs Tesseract and poppler installed on the system.

| | Official PDF skill | This hook |
|---|---|---|
| Triggers when | The model judges the request to be about PDFs | Every `Read` on a `.pdf`; no judgement involved |
| Steps to get the text | Load the skill → run a script → Read the `.txt` | One `Read` |
| When the native Read fails | Only the model sees the error; it may think of the skill, or may give up | A text PDF never reaches poppler |
| Dependencies | pdfplumber, pypdf, reportlab, qpdf, pytesseract, pdf2image, Tesseract, poppler | `pypdfium2`, and nothing else |
| Scope | Read, write and modify PDFs | Read only |

They are complementary, not substitutes. To *read* a PDF, this hook makes `Read` itself do the right thing; to *do something to* a PDF, use the skill; installing both is reasonable. The one person the skill already covers is the one who says "use the pdf skill to extract the text" every single time — for them this hook adds nothing.

## What it does

The hook runs before every `Read`, does nothing unless the path ends in `.pdf` and the file exists, and then:

| Situation | Route |
|---|---|
| Text layer on every page | Rewrite the Read to a UTF-8 `.txt` (`updatedInput`). One tool call, done. |
| Text layer on most pages | Same, with every page classed as text, blank, image or figure (below). The note attached to the Read lists each non-text class separately, so Claude skips the blank pages and goes back for the image and figure pages with a page range. |
| Scan (fewer than 50 characters in the whole file, or more than half of its pages under 15), native render available | Allow — vision *is* the right tool for a scan. |
| Scan, poppler missing (over 10 pages, or a page range) | Render the pages here at 1.5× (about 108 DPI) and point the Read at the first PNG, the rest listed in the note: up to 20 pages of an explicit range; the first 10 of a long scan read with no range. |
| Short explicit range (≤3 pages, and fewer than the whole document) | You want to *see* the layout. Allow, or render locally if poppler is absent. |
| Page range outside the document, or a `pages` value that does not parse (`N`, `N-M` and comma lists like `1,3,5` are accepted) | Deny, and say why, instead of letting Claude hit an unrelated poppler error. |
| Anything unexpected — unreadable or password-protected file, zero pages, no engine, only `pypdf` when a render is needed, hook crash | Allow. A hook must never be the reason a Read fails. When the cause is a missing package rather than a broken file, the allowed Read carries a note naming the `pip install` that fixes it, at most once a week (below). |

If the extraction is longer than one Read shows (over 1,800 lines or about 40,000 tokens), the rewritten Read returns a preview worth about 3,000 tokens rather than a fixed number of lines: the line count is computed from that file's own measured token density, clamped to between 40 and 400 lines, so the first look costs the same context whatever the document is. Across the 27 text files in the corpus that lands between 203 and 400 lines. Density is a property of the file, not of its language — the 1,039-page English bill measures 10.64 tokens per line and the Chinese population report 10.26, eleven lines apart. The note says how many lines were shown and points at `Grep` or `offset`/`limit` rather than handing back a silent prefix. A Read with `pages="100-105"` on any text PDF lands on those pages: the hook sets `offset`/`limit` to the span between their `[page N/M]` markers. Your own `offset`/`limit`, if you pass them, are left alone.

**Page classes.** Each page is classed from its character count (after control characters and private-use glyphs are stripped) and the share of its area covered by image objects: *blank* is under 15 characters and under 5% image; *image* is under 15 characters with an image on it, or a text layer that stripped to nothing (a symbol font); *figure* is under 200 characters and at least 40% image; everything else is *text*. The `.txt` marks each non-text page on its `[page N/M]` line — `(blank page)`, `(image page, no text layer)`, `(image page, text layer unreadable)`, `(figure page: 45% image, 86 chars)` — and the note attached to the Read lists the three classes separately, at most 20 pages each: blank pages are something to skip; image and figure pages come with a `pages="N"` hint so Claude can go and look at one. On the 110-page 國發會 population report in the corpus that is 15 truly blank pages and 2 full-page images. Known limit: a vector drawing — a map or a chart drawn as paths, like page 28 of `zenodo/22072701` — has an image area near zero and is classed as text. With `pypdf`, which cannot report image areas, the rule falls back to characters alone: a page under 15 characters is called an image page, never blank, so Claude goes and looks rather than skipping something that was there. The document-level verdict — scan or not — is unchanged by any of this.

## Install · Verify · Uninstall

```
/plugin marketplace add WatsonTsai/pdf-text-router
/plugin install pdf-text-router@pdf-text-router
pip install pypdfium2
```

Python 3.9 or newer (CI runs 3.9 and 3.12). That one package is the whole dependency list: rendering needs no imaging library, because the hook writes its PNGs with the standard library (see [Engines](#design-notes)).

Then verify the hook is actually live. This matters: a hook that fails open is indistinguishable from a hook that never loaded.

```
python ~/.claude/plugins/marketplaces/pdf-text-router/hooks/pdf_text_router.py --selftest
```

(If your plugin root is elsewhere, `/plugin` shows the installed path.) The self-test reports a one-line status — everything works, extract-only (`pypdf` alone: no local rendering for scans), or no engine at all, in which case it exits non-zero — along with the active engine, whether `pdftoppm` is on your PATH, the mode, and the cache directory with its usage and limits. `--check FILE.pdf` prints the routing decision for one file without side effects.

**Uninstall**

```
/plugin uninstall pdf-text-router@pdf-text-router
python ~/.claude/plugins/marketplaces/pdf-text-router/hooks/pdf_text_router.py --clear-cache
```

or simply delete `~/.claude/pdf-text-cache`. If you installed by hand, also remove the `PreToolUse` entry whose `matcher` is `Read` and whose `command` points at `pdf_text_router.py` from `~/.claude/settings.json`.

<details>
<summary><b>Manual install</b></summary>

<br>

Copy `hooks/pdf_text_router.py` into `~/.claude/hooks/`, then merge `examples/settings.snippet.unix.json` (or `.windows.json`) into `~/.claude/settings.json`, replacing `YOUR_USERNAME`. The `command` field says `python`; change it to `python3` or an absolute interpreter path if that is not what your PATH resolves to.

</details>

## Numbers

Measured over [35 public PDFs](scripts/corpus.json) — all downloadable without registration, all under ODC-BY, CC-BY, or US/JP government terms. 27 have a text layer; 8 are scans (checked by eye, page by page; one "scan" is a PDF whose text layer is entirely private-use font codes, and sending it to vision is the right call). Full rows: [`scripts/results/2026-08-26.csv`](scripts/results/2026-08-26.csv); the console output is next to it.

Claude Code takes one of two paths, and they do not cost the same, so the tables are split by path.

**Document block path (≤10 pages).** The API bills every page as an image *and* as text, so extracting the text yourself can never lose — the ratio is `(image + text) / text`.

| File | Pages | Chars/page | Ratio |
|---|---:|---:|---:|
| loc/PV2UUKY4… (dot-gov) | 1 | 1,065 | 3.56× |
| loc/6LM5MPDE… | 2 | 1,097 | 3.49× |
| loc/ZZLARWOC… | 2 | 1,106 | 3.31× |
| loc/PRMLNHO3… | 2 | 1,192 | 3.29× |
| olmocr/headers_footers/05d9… | 1 | 934 | 3.23× |
| zenodo/22072300 (slides) | 9 | 1,899 | 2.44× |
| loc/73KIZM2K… | 1 | 2,273 | 2.20× |
| olmocr/old_scans_math/2_pg39 | 1 | 682 | 2.16× |
| soumu/n1110000 (JP) | 2 | 1,150 | 1.99× |
| olmocr/headers_footers/0387… | 1 | 2,688 | 1.95× |
| olmocr/arxiv_math/2503.03762 | 1 | 2,873 | 1.89× |
| olmocr/arxiv_math/2503.03855 | 1 | 2,954 | 1.86× |
| olmocr/tables/1529… | 1 | 3,044 | 1.84× |
| olmocr/multi_column/03cc… | 1 | 3,192 | 1.80× |
| olmocr/arxiv_math/2503.02004 | 1 | 3,719 | 1.73× |
| olmocr/multi_column/0083… | 1 | 5,270 | 1.48× |
| **Pooled (16 files)** | | | **2.25×** |

**Poppler path (>10 pages).** Claude Code runs `pdftoppm -jpeg -r 100` and sends images only, so here the text layer can cost more than the pictures.

| File | Pages | Chars/page | Image tokens | Text tokens | Ratio |
|---|---:|---:|---:|---:|---:|
| zenodo/22072701 (slides) | 32 | 250 | 41,472 | 2,003 | 20.70× |
| zenodo/22069734 (slides) | 26 | 330 | 32,240 | 2,149 | 15.00× |
| zenodo/22070313 (slides) | 16 | 372 | 19,840 | 1,492 | 13.30× |
| zenodo/22070195 (slides) | 21 | 449 | 26,040 | 2,359 | 11.04× |
| govinfo/BILLS-117hr3684enr | 1,039 | 2,729 | 2,019,816 | 709,308 | 2.85× |
| govinfo/BILLS-117hr5376enr | 273 | 2,742 | 530,712 | 187,211 | 2.83× |
| ndc/population-projection-2024 (ZH) | 110 | 1,515 | 138,600 | 52,720 | 2.63× |
| soumu/n1210000 (JP) | 14 | 863 | 17,640 | 7,479 | 2.36× |
| soumu/n2110000 (JP) | 11 | 1,203 | 13,860 | 6,610 | 2.10× |
| loc/IMJ4MPPY… (dot-gov) | 143 | 3,545 | 177,320 | 126,741 | 1.40× |
| loc/QSPE3BHA… (dot-gov) | 83 | 8,346 | 102,920 | 173,187 | **0.59×** |
| **Pooled (11 files)** | | | 3,120,460 | 1,271,259 | **2.45×** |

Break-even on the poppler path: one letter-size page at 100 DPI is about 1,240 image tokens, so the text layer only costs more once a page carries more than ~5,000 Latin characters or ~1,600 CJK characters.

By category, pooled over the 27 files with a text layer:

| Category | Pooled ratio |
|---|---|
| Slide decks (5, Zenodo CC-BY) | **10.59×** |
| Legislation (2, govinfo) | 2.84× |
| CJK white papers and reports (4, 総務省 + 國發會) | 2.53× |
| Other (3: headers/footers pages, one old math scan with a text layer) | 2.26× |
| Dense single pages (6, arXiv / multi-column / tables) | 1.74× |
| Government documents (7, LoC dot-gov) | 0.95× — six of the seven land between 1.40× and 3.56× (1.43× pooled); the seventh, `loc/QSPE3BHA…`, is 83 pages at 8,346 characters per page and comes out at 0.59×, which drags the group under 1 |
| All 27 with a text layer | 2.45× (best 20.70×, median 2.36×, worst 0.59×) |

Sparse pages (under 1,500 characters) averaged 6.58×; dense ones 1.96×.

**What the estimate assumes.** Both token columns are estimates, not billing records.

- Text: 4 characters per token for Latin script, 1.3 for CJK, counted over the extracted text after stripping control characters.
- Image: the [documented vision formula](https://docs.anthropic.com/en/docs/build-with-claude/vision), `ceil(width/28) × ceil(height/28)` tokens per image, with the long edge capped at 2,576 px (where the API downscales) and no single image billed above 4,784 tokens. The benchmark calls the hook's own `est_image_tokens()`, so the tables and what the hook tells Claude cannot drift apart.
- 100 DPI on the poppler path is what Claude Code passes to `pdftoppm` (`-r 100`, reported in [#23704](https://github.com/anthropics/claude-code/issues/23704)).
- The document block path's render resolution is not published. The tables use the page's own point size (72 DPI) as a lower bound; Anthropic's own figure of [1,500–3,000 tokens per page](https://docs.anthropic.com/en/docs/build-with-claude/pdf-support) is the upper bound. At 72 DPI a letter page is about 640 image tokens plus its text — below the bottom of Anthropic's range, so the document block ratios above are if anything understated.
- That Claude Code uses the document block for ≤10-page files, rather than rendering them itself, is inferred from the collaborator's comment on #23704, not from source.
- Page size is read from the first page and applied to all of them.
- "Scan" versus "text" is the hook's own verdict (`classify()`), so a file the table calls a scan is one the hook would send to vision.

And the slide-deck caveat, in numbers: the 20.70× best case is a 32-page deck with 23 pages under 200 characters — the station names and scale bars on its maps are not in the text layer; page 9 of `zenodo/22070313` has a text layer that consists of the page number, with everything else burned into an image. The hook marks page 9 as an image page and page 8 (86 characters, 45% image) as a figure page; in the 32-page deck it marks ten pages as figure pages, and the map drawn as vector paths on page 28 it does not.

Reproduce it:

```
python scripts/fetch_corpus.py                                   # the 35 files, verified by sha256
python scripts/benchmark.py corpus --manifest scripts/corpus.json --csv scripts/results/today.csv
python scripts/benchmark.py ~/docs --anonymize                   # your own files, safe to paste into an issue
```

## Why this exists

The problem is not mine to claim. [`anthropics/claude-code#23704`](https://github.com/anthropics/claude-code/issues/23704) — *"Read tool's PDF support requires poppler-utils but it's undocumented, usually absent, and not detected after install"* — has been open since February 2026, with 20 👍 and reports on Linux, macOS and Windows. On 2026-08-17 an Anthropic collaborator confirmed the shape of it:

> Short PDFs (10 pages or fewer) are read without poppler; only page-range reads of longer PDFs need `pdftoppm`/`pdfinfo` from poppler-utils. **The docs still don't mention that requirement, so we're keeping this open as a documentation item.**

The original reporter's own workaround is the thing this hook automates: *"the workaround is `curl` + `pdftotext` + `Read` on the resulting text file — 3 tool calls for what should be a single `Read`."*

There is a second issue underneath. [#23699](https://github.com/anthropics/claude-code/issues/23699) — Read errors are not surfaced in the TUI — is **closed and marked stale**. So when this happens, you do not see `pdftoppm is not installed`. You see Claude fail to read a PDF and start guessing.

**If Anthropic adds a text-first mode to the Read tool, uninstall this.** That is the outcome to hope for; #23704 is the place to push for it.

## Design notes

**Engines.** `pypdfium2` (BSD-3-Clause / Apache-2.0) is the default. `pymupdf` is used if already installed, but is never a dependency — it is AGPL-3.0, which is a problem for exactly the people who care most about token cost. `pypdf` is a last resort: it cannot render, and its extraction does not agree with `pypdfium2`'s on complex layouts — on the four CJK files in the corpus its CJK character count differs from `pypdfium2`'s by −0.8% to +17% (`pypdf` 6.13), so a verdict or a cache built by one engine is not interchangeable with the other's. Rendering pulls in no third-party imaging library: a PNG is a signature, three chunks and a zlib stream, so the hook writes pdfium's pixel buffer out itself with `zlib` and `struct` (MuPDF never needed it either — `fitz.Pixmap.save()` writes PNG on its own). Measured against the `pillow` path it replaced, that is 22% faster and produces files 42% larger — identical pixels, but every row is written with filter 0 where `pillow` picks a filter per row. The only thing the extra size costs is disk in the cache, which reaches the 500 MB prune limit sooner; nothing about it reaches the model. Compressing harder is not worth it: zlib level 9 saves another 10% and takes three times as long, so the writer stays at level 6.

**Missing engine.** With no engine importable the hook used to load, decide nothing and allow every Read — outwardly identical to not being installed, which is how an install can look fine for months while doing nothing. It still allows the Read, but now attaches a note telling the model that pdf-text-router is installed and inert, that `pip install pypdfium2` is what fixes it, and that this particular Read is proceeding exactly as it would have without the hook. The same happens when `pypdf` is the only engine and the page in question has to be rendered — extraction works, scans do not. Both notes are throttled to one every 7 days by a `.install-notice` marker in the cache directory; if that directory cannot be written to, nothing is said at all, since a note on every Read would be worse than silence. A file that merely failed to parse stays silent — a broken PDF is not a missing package, and saying so would send you shopping for a fix that does not exist. Known wrinkle: `--clear-cache` and the pruner delete `.install-notice` along with everything else in the directory, so the seven days start again; the worst case is one extra note.

**Cache.** `~/.claude/pdf-text-cache`, override with `PDF_TEXT_ROUTER_CACHE`. Entries are plain text: `<key>-<name>.txt` is what Claude reads; `<key>-<name>.json` next to it is a sidecar (page count, characters per page before and after cleaning, image area per page, page size, engine, token estimate, the line each page marker sits on, and the key) that lets a cache hit be decided without opening the PDF; `<key>-p<N>-1.5x-<name>.png` are rendered pages. A sidecar written by an earlier version lacks `page_image_area`, `page_raw_chars` or `key`; it is not trusted, and the file is extracted once more. The key is the file's size, mtime and a hash of its first and last 256 KB, not `stat()` alone: regenerating a PDF often lands in the same second at the same byte count, and a stat-keyed cache would then serve the previous version's text while the Read that would have caught it is rewritten away. Every entry is written to a temp file and renamed, and a sidecar is only trusted next to a complete `.txt`, because a truncated extraction is worse than none. Pruning runs on every write: anything older than 30 days goes, then the oldest entries until the directory is under 500 MB (`PDF_TEXT_ROUTER_CACHE_MAX_MB`), the files just written excepted. Age is measured from when the entry was written — a cache hit does not touch it — so an entry expires 30 days after extraction whether or not it was read yesterday. `--clear-cache` empties the directory on demand.

On privacy: the PDF was already a readable file on this machine, so the cache adds exposure only when the PDF sits somewhere your home directory is not — an encrypted volume, an external drive, a network share. `PDF_TEXT_ROUTER_CACHE=beside` writes the cache next to the PDF instead: `report.pdf.txt`, `report.pdf.json`, `report.pdf.p3.1.5x.png`, with no key in the name (the sidecar carries it and is checked on every hit; a PNG older than its PDF is rendered again). If the PDF's folder cannot be written to, that file falls back to the central directory and one line goes to stderr. Nothing beside a PDF is ever pruned — that is your folder, not the hook's — and `--clear-cache` in beside mode deletes nothing; it prints where the files are. There is still no encrypted mode.

**Modes.** `PDF_TEXT_ROUTER_MODE=rewrite` (default) uses the hook's `updatedInput` to point the Read at the `.txt` (or the first `.png`), so it costs one tool call; any other value is read as `rewrite`. `PDF_TEXT_ROUTER_MODE=deny` is the fallback for Claude Code builds that predate `updatedInput`: the Read is denied with the `.txt` or `.png` paths in the reason, and Claude reads them on the next call. Out-of-range and unparseable `pages` are denied in both modes — there is nothing to redirect to.

**Environment.** `PDF_TEXT_ROUTER_MODE` (`rewrite` | `deny`), `PDF_TEXT_ROUTER_CACHE` (a directory, or the word `beside` to cache next to each PDF; see Cache), `PDF_TEXT_ROUTER_CACHE_MAX_MB` (default 500; central directory only), `PDF_TEXT_ROUTER_SCALE` (render scale for local PNGs, 1.0–3.0, default 1.5 ≈ 108 DPI; values outside the range are clamped).

**Timeout.** 60 s, set in `hooks/hooks.json`. A hook that overruns is killed and has made no decision, so a pathological PDF degrades to the native path rather than to a blocked Read; the half-written entry is never trusted, since the `.txt` is renamed into place only when complete and the sidecar is written after it. On the 1,039-page bill the first extraction takes 2.1–2.3 s; the cached second Read takes 0.3 s and does not open the PDF.

**Platform status.** The hook's behaviour inside Claude Code has been verified on Windows (Claude Code 2.1.241). The unit tests run on Linux, macOS and Windows in CI; the end-to-end hook path on macOS and Linux has not been exercised by me — if you run it there, an issue saying "works" is as useful as one saying "does not".

**Relation to the official PDF skill.** See [Compared with the official PDF skill](#compared-with-the-official-pdf-skill) above.

## Tests

```
python -m pytest tests -q
```

The unit tests need no sample files — the fixtures are minimal PDFs built in memory with computed xref tables. `tests/test_corpus.py` adds seven checks against the 35 benchmark files (every hand-verified scan is classified as one, every other file is text, the 國發會 report names its blank and image pages, known pages get their class, the vector map is an accepted miss, the routing dry-run agrees) and is skipped when `corpus/` has not been fetched: 182 passed without it, 189 with it. Several tests exist because an earlier version of this suite passed while the thing it claimed to test was broken; those say so in the test. The same suite plus `--selftest` runs on every push across three OSes and two Python versions, 3.9 and 3.12 ([workflow](.github/workflows/test.yml)).

## License

MIT
