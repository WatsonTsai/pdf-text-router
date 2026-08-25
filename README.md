# pdf-text-router

A Claude Code hook that routes each `Read` on a PDF to the path that actually works.

English · [繁體中文](README.zh-TW.md)

Claude Code reads a PDF by rendering its pages to images. For a scan that is the right call — vision is the OCR engine. For anything with a real text layer it means paying for pictures of words. And for PDFs longer than ten pages it means shelling out to poppler's `pdftoppm`, a binary the official setup docs have never mentioned.

This hook intercepts the `Read` and picks a different route.

## The problem is not mine to claim

[`anthropics/claude-code#23704`](https://github.com/anthropics/claude-code/issues/23704) — *"Read tool's PDF support requires poppler-utils but it's undocumented, usually absent, and not detected after install"* — has been open since February 2026, with 20 👍 and reports on Linux, macOS and Windows. On 2026-08-17 an Anthropic collaborator confirmed the shape of it:

> Short PDFs (10 pages or fewer) are read without poppler; only page-range reads of longer PDFs need `pdftoppm`/`pdfinfo` from poppler-utils. **The docs still don't mention that requirement, so we're keeping this open as a documentation item.**

The original reporter's own workaround is the thing this hook automates: *"the workaround is `curl` + `pdftotext` + `Read` on the resulting text file — 3 tool calls for what should be a single `Read`."*

There is a second issue underneath. [#23699](https://github.com/anthropics/claude-code/issues/23699) — Read errors are not surfaced in the TUI — is **closed and marked stale**. So when this happens, you do not see `pdftoppm is not installed`. You see Claude fail to read a PDF and start guessing.

## What it does

| Situation | Route |
|---|---|
| Text layer present | deny the Read, extract to a UTF-8 `.txt`, hand Claude that path |
| Scan, native render available | allow — vision *is* the right tool for a scan |
| Scan, no poppler | deny, render the pages here, hand Claude the PNGs |
| Short explicit range (≤3 pages) | you want to *see* the layout: allow, or render locally if poppler is absent |
| Page range outside the document | say so, instead of letting Claude hit an unrelated poppler error |
| Anything unexpected | allow — a hook must never be the reason a Read fails |

The extracted text carries `[page N/M]` markers, so Claude can still cite page numbers. If the extraction is longer than Read shows in one call, the hook says so and asks for `Grep` or `offset`/`limit` rather than handing back a silent prefix.

## Install

```
/plugin marketplace add WatsonTsai/pdf-text-router
/plugin install pdf-text-router@pdf-text-router
pip install pypdfium2 pillow
```

Then verify it is actually live — this matters, because a hook that fails open is indistinguishable from a hook that never loaded:

```
python ~/.claude/plugins/.../hooks/pdf_text_router.py --selftest
```

It reports the active engine, whether rendering is available, and whether `pdftoppm` is on your PATH. `--check FILE.pdf` prints the routing decision for one file without side effects.

<details>
<summary><b>Manual install</b></summary>

<br>

Copy `hooks/pdf_text_router.py` into `~/.claude/hooks/`, then merge `examples/settings.snippet.unix.json` (or `.windows.json`) into `~/.claude/settings.json`, replacing `YOUR_USERNAME`. The `command` field says `python`; change it to `python3` or an absolute interpreter path if that is not what your PATH resolves to.

</details>

### Engines

`pypdfium2` (BSD-3-Clause / Apache-2.0) is the default. `pymupdf` is used if already installed, but is never a dependency — it is AGPL-3.0, which is a problem for exactly the people who care most about token cost. `pypdf` is a last resort: it cannot render, and on a two-column report it dropped 7.7% of the Chinese characters that the other two both recovered.

`pillow` is only needed for the rendering path. If you never read scans without poppler, you never need it.

## Where this helps, and where it does not

The saving scales inversely with how much text each page carries. Measured over [35 public PDFs](scripts/corpus.json) — all downloadable without registration, all under ODC-BY, CC-BY, or US/JP government terms:

| Category | Pooled ratio |
|---|---|
| Slide decks (5, Zenodo CC-BY) | **10.71×** |
| Legislation (2, govinfo) | 2.93× |
| CJK white papers and reports (4, 総務省 + 國發會) | 2.59× |
| Dense single pages (6, arXiv / multi-column / tables) | 1.74× |
| **Government documents (7, LoC dot-gov)** | **0.96× — this set was a net loss** |
| All 27 with a text layer | 2.51× |

Sparse pages (under 1500 characters) averaged 6.64×; dense ones 1.98×.

**So: if you mostly read dense, text-heavy documents, this hook will save you very little, and on individual files it can cost you more than it saves — the worst file in that corpus came out at 0.60×.** What it still does on those files is keep them readable when poppler is missing, which above ten pages is the difference between a document and an error message.

Reproduce it on your own documents:

```
python scripts/fetch_corpus.py          # the 35 public files, verified by sha256
python scripts/benchmark.py corpus/     # or point it at your own directory
python scripts/benchmark.py ~/docs --anonymize   # safe to paste into an issue
```

Both token columns are estimates, not billing records: text is counted at 1.3 characters per token for CJK and 4 for everything else, images at `(width × height) / 750`, following whichever path Claude Code would actually take for that file.

## Notes

The cache lives in `~/.claude/pdf-text-cache` (override with `PDF_TEXT_ROUTER_CACHE`). It is keyed on file content, not on `stat()` — regenerating a PDF often lands in the same second at the same byte count, and a stat-keyed cache would then serve the previous version's text while the real Read stays denied, where nobody can see it. Entries are written to a temp file and renamed, because Claude Code kills a hook that overruns its timeout and a truncated extraction is worse than none. Nothing prunes the cache; delete the directory whenever you like.

The hook is complementary to the official [`anthropics/skills` PDF skill](https://github.com/anthropics/skills/tree/main/skills/pdf). That one is about *doing things to* PDFs — merging, splitting, filling forms. This one is about *getting one into context*.

**If Anthropic adds a text-first mode to the Read tool, uninstall this.** That is the outcome to hope for; #23704 is the place to push for it.

## Tests

```
python -m unittest discover -s tests
```

59 tests, no sample files needed — the fixtures are minimal PDFs built in memory with computed xref tables. Several of them exist because an earlier version of this suite passed while the thing it claimed to test was broken; those say so in the test.

## License

MIT
