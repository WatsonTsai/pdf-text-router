# pdf-text-router

一支 Claude Code hook，把每一次對 PDF 的 `Read` 導向真正走得通的那條路。

[English](README.md) · 繁體中文

你請 Claude Code 讀一份 PDF。沒有任何錯誤訊息，但 Claude 悄悄換了方法——改用一行 `Bash`、或憑檔名開始猜——或者直接告訴你檔案打不開。底下真正發生的事是 `pdftoppm is not installed`，而你從來看不到它，因為 Read 的錯誤不會顯示在 TUI 裡（[#23699](https://github.com/anthropics/claude-code/issues/23699)，已以 stale 關閉）。任何超過十頁的 PDF、任何帶頁碼範圍的 Read，都會經過 poppler 的 `pdftoppm`，而官方安裝文件從來沒有提過這件事（[#23704](https://github.com/anthropics/claude-code/issues/23704)，仍開啟中）。就算 poppler 真的在，一份有文字層的 PDF 也還是被當成「文字的照片」在讀。

**你會得到什麼。** 對有文字層的 PDF，hook 把那次 Read 就地改寫：一次 `Read`，回來的是帶 `[page N/M]` 標記的 UTF-8 `.txt`，所以 Claude 仍然引用得了頁碼、可以對檔案 `Grep`，而且不會對本來就是文字的東西再跑一次 OCR。全程不經 poppler。對掃描件，原生路徑走得通就放行；會因為缺 poppler 而失敗時，hook 在本機把頁面渲成 PNG，把同一次 Read 指向第一張，其餘列在說明裡。

**你要付出什麼。** 抽出的文字以明文快取在 `~/.claude/pdf-text-cache`。PDF 本身就是同一台機器上一份讀得到的檔案，所以快取多出來的暴露只在一種情境：PDF 放在加密磁碟、外接碟或網路位置，而快取落到本機家目錄。針對這種情況，`PDF_TEXT_ROUTER_CACHE=beside` 讓快取跟著 PDF 走，或把這個變數指到你自己選的目錄；仍然沒有加密模式（見[快取](#設計說明)）。投影片是省最多的一類，也是最大的陷阱：畫在圖裡的文字——地圖上的測站名、比例尺、整張匯出成圖片的投影片——不在文字層裡。hook 會依字元數與影像物件佔頁面的面積比，把每一頁分成 text、blank、image、figure 四類，所以「整張是一張圖」的投影片和「大圖配一行說明」的頁它標得出來；標不出來的是向量繪圖——用路徑畫出來的地圖或圖表，影像面積接近零，看起來就像一頁字很少的文字頁。第一次讀大檔案要付抽取的時間（語料裡那份 1,039 頁的法案用 `pypdfium2` 要 2.1–2.3 秒）；之後每一次 Read 都是快取命中，完全不開 PDF（同一份檔案 0.3 秒）。

## 這適合你嗎？

兩個問題。

1. `which pdftoppm`（Unix）或 `where pdftoppm`（Windows）。如果什麼都沒印出來，Claude Code 目前讀不了任何超過十頁的 PDF，也讀不了任何頁碼範圍；這支 hook 就是「一份文件」與「一則錯誤訊息」的差別。
2. `python scripts/benchmark.py <你自己的 PDF 資料夾>`。它會逐檔印出 Claude Code 在圖片上會花多少、文字要花多少，以及一個中位數。

怎麼判讀：

- 中位數比值高於 1.5×——裝吧，光是省下的 token 就值回票價。
- 你的頁面每頁超過約 5,000 個拉丁字元（或約 1,600 個 CJK 字元）——在 poppler 路徑上，文字層會比圖片**更貴**。你仍然得到的是可 `grep`、無損的文字，以及一份在缺 poppler 時仍打得開的文件。用這個來決定，不要用 token。

如果你已經把 PDF 先過 `pdftotext`、或某個會預先處理文件的 MCP server，這支不適合你——你已經在上一層解決了同一個問題。（官方的 PDF skill 是另一回事，見下一節。）如果你無法接受「讀過的所有東西都有一份明文快取」，也不適合你；它沒有加密模式。

## 與官方 PDF skill 的分工

Anthropic 的 [`anthropics/skills` PDF skill](https://github.com/anthropics/skills/tree/main/skills/pdf) 是模型判斷「這個請求跟 PDF 操作有關」才載入，然後跑 Bash 腳本（pdfplumber、pypdf、reportlab、qpdf）；它不改 `Read` 工具的行為。涵蓋範圍是抽文字、抽表格、合併、分割、旋轉、填表單、浮水印、加密、建立新 PDF，以及用 pytesseract + pdf2image 做 OCR（需要系統裝好 Tesseract 與 poppler）。

| | 官方 PDF skill | 這支 hook |
|---|---|---|
| 觸發時機 | 模型判斷這個請求跟 PDF 有關 | 每一次對 `.pdf` 的 `Read` 必經，不靠判斷 |
| 拿到文字要幾步 | 載入 skill → 跑腳本 → Read 那份 `.txt` | 一次 `Read` |
| 原生 Read 失敗時 | 錯誤只有模型看得到；它可能想到用 skill，也可能放棄 | 有文字層的 PDF 根本不會走到 poppler |
| 相依 | pdfplumber、pypdf、reportlab、qpdf、pytesseract、pdf2image、Tesseract、poppler | `pypdfium2`，沒有別的 |
| 範圍 | 讀、寫、改 PDF | 只讀 |

兩者互補，不互相取代。要「讀」一份 PDF，這支 hook 讓 `Read` 本身就做對；要「對 PDF 做事」，用官方 skill；兩個都裝很合理。唯一一種有了官方 skill 就不需要這支的人，是每一次都會明講「用 pdf skill 抽文字」的人——對他們來說這支 hook 沒有多給什麼。

## 它做什麼

hook 在每一次 `Read` 之前執行，路徑不是 `.pdf` 結尾、或檔案不存在，就什麼都不做；是的話：

| 情境 | 走法 |
|---|---|
| 每一頁都有文字層 | 把 Read 改寫成指向 UTF-8 `.txt`（`updatedInput`）。一次工具呼叫，完成。 |
| 大部分頁面有文字層 | 同上，並把每一頁分成 text、blank、image、figure 四類（見下）。附給 Read 的說明會把非文字的三類分開列出，讓 Claude 跳過空白頁，帶頁碼範圍回頭去看 image 與 figure 頁。 |
| 掃描件（整份不到 50 個字元，或超過一半的頁面少於 15 個字元），原生渲染可用 | 放行——掃描件本來就該交給 vision。 |
| 掃描件，缺 poppler（超過 10 頁，或帶頁碼範圍） | 在本機以 1.5×（約 108 DPI）渲染頁面，把 Read 指向第一張 PNG、其餘列在說明裡：明確範圍最多 20 頁；沒帶範圍的長掃描件給前 10 頁。 |
| 明確的短範圍（≤3 頁，且少於整本） | 你是想「看」版面。放行；沒有 poppler 就本機渲染。 |
| 頁碼超出文件範圍，或 `pages` 的值解析不了（接受 `N`、`N-M` 與 `1,3,5` 這種逗號清單） | deny 並說明原因，而不是讓 Claude 撞上一個與此無關的 poppler 錯誤。 |
| 任何意外——檔案讀不了或有密碼、零頁、沒有引擎、需要渲染時只有 `pypdf`、hook 自己崩潰 | 放行。hook 絕不該成為 Read 失敗的理由。如果原因是缺套件而不是檔案壞掉，這次放行的 Read 會附上一則說明，點名該跑哪一行 `pip install`，每週最多一次（見下）。 |

如果抽出的內容長到 Read 一次顯示不完（超過 1,800 行或約 40,000 個 token），改寫後的 Read 回傳的不是固定行數，而是約 3,000 個 token 的預覽：行數由這份檔案自己實測的 token 密度換算，夾在 40 行到 400 行之間，所以不管讀的是哪一種文件，第一眼花掉的 context 都一樣多。在語料裡 27 份有文字層的檔案上，這個值落在 203 到 400 行之間。密度是檔案的性質、不是語言的性質——那份 1,039 頁的英文法案每行 10.64 個 token，中文的人口推估報告 10.26，兩者只差十一行。說明裡會講明這次顯示了幾行，並指向 `Grep` 或 `offset`／`limit`，而不是默默交出一段開頭。對任何有文字層的 PDF 帶 `pages="100-105"` 的 Read 會直接落在那幾頁：hook 把 `offset`／`limit` 設成那幾頁 `[page N/M]` 標記之間的區段。你自己傳的 `offset`／`limit` 則原封不動。

**頁面分類。** 每一頁依字元數（去除控制字元與私用區字形之後）與影像物件佔頁面的面積比分類：*blank* 是少於 15 個字元且影像不到 5%；*image* 是少於 15 個字元但頁上有影像，或是有字形但清完為空的文字層（符號字型）；*figure* 是少於 200 個字元且影像至少 40%；其餘都是 *text*。`.txt` 會在每一個非文字頁的 `[page N/M]` 那行加上標記——`(blank page)`、`(image page, no text layer)`、`(image page, text layer unreadable)`、`(figure page: 45% image, 86 chars)`——附給 Read 的說明則把三類分開列，每類最多 20 頁：blank 頁是叫模型跳過的；image 與 figure 頁附一個 `pages="N"` 的提示，讓 Claude 可以去看其中一頁。語料裡那份 110 頁的國發會人口推估報告，就是 15 頁真空白加 2 頁整頁圖。已知極限：向量繪圖——用路徑畫出來的地圖或圖表，例如 `zenodo/22072701` 第 28 頁——影像面積接近零，會被歸成 text。用 `pypdf` 時算不出影像面積，規則退回純字元：少於 15 個字元的頁一律算 image、不算 blank，讓 Claude 去看一眼，而不是跳過一頁本來有東西的頁。整份文件「是不是掃描件」的判定不受這些影響。

## 安裝 · 驗證 · 解除安裝

```
/plugin marketplace add WatsonTsai/pdf-text-router
/plugin install pdf-text-router@pdf-text-router
pip install pypdfium2
```

需要 Python 3.9 以上（CI 跑 3.9 與 3.12）。相依清單就這一個套件：渲染不需要任何影像函式庫，PNG 是 hook 自己用標準函式庫寫出來的（見[引擎](#設計說明)）。

接著確認 hook 真的生效。這件事很重要：一支 fail-open 的 hook「沒載入」和「有載入但放行」在外觀上完全一樣。

```
python ~/.claude/plugins/marketplaces/pdf-text-router/hooks/pdf_text_router.py --selftest
```

（如果你的 plugin 根目錄在別處，`/plugin` 會顯示安裝路徑。）self-test 會回報一行狀態——全部可用、只能抽文字（只有 `pypdf`：掃描件沒有本機渲染的退路），或一個引擎都沒有（這時會以非零狀態碼結束）——以及使用中的引擎、`pdftoppm` 在不在 PATH 上、目前的模式，還有快取目錄與它的用量和上限。`--check FILE.pdf` 則會印出單一檔案的路由決策，不產生任何副作用。

**解除安裝**

```
/plugin uninstall pdf-text-router@pdf-text-router
python ~/.claude/plugins/marketplaces/pdf-text-router/hooks/pdf_text_router.py --clear-cache
```

或者直接刪掉 `~/.claude/pdf-text-cache`。如果你是手動安裝的，還要從 `~/.claude/settings.json` 移除那條 `matcher` 為 `Read`、`command` 指向 `pdf_text_router.py` 的 `PreToolUse` 項目。

<details>
<summary><b>手動安裝</b></summary>

<br>

把 `hooks/pdf_text_router.py` 複製到 `~/.claude/hooks/`，再把 `examples/settings.snippet.windows.json`（或 `.unix.json`）合併進 `~/.claude/settings.json`，並把 `YOUR_USERNAME` 換成實際路徑。`command` 欄位寫的是 `python`；如果你的 PATH 解析結果不是它，改成 `python3` 或直譯器的絕對路徑。

</details>

## 數字

在 [35 份公開 PDF](scripts/corpus.json) 上實測——全部免註冊可下載，授權為 ODC-BY、CC-BY 或美日政府條款。其中 27 份有文字層、8 份是掃描件（逐頁用眼睛核對過；有一份「掃描件」其實是文字層全為私用區字型碼的 PDF，把它交給 vision 是對的）。完整資料列在 [`scripts/results/2026-08-26.csv`](scripts/results/2026-08-26.csv)，主控台輸出放在旁邊。

Claude Code 會走兩條路之一，兩條路的成本不同，所以表格依路徑分開。

**document block 路徑（≤10 頁）。** API 對每一頁同時計圖片與文字的 token，所以自己抽文字永遠不會虧——比值是 `（圖片 + 文字）／文字`。

| 檔案 | 頁數 | 每頁字元 | 比值 |
|---|---:|---:|---:|
| loc/PV2UUKY4…（dot-gov） | 1 | 1,065 | 3.56× |
| loc/6LM5MPDE… | 2 | 1,097 | 3.49× |
| loc/ZZLARWOC… | 2 | 1,106 | 3.31× |
| loc/PRMLNHO3… | 2 | 1,192 | 3.29× |
| olmocr/headers_footers/05d9… | 1 | 934 | 3.23× |
| zenodo/22072300（投影片） | 9 | 1,899 | 2.44× |
| loc/73KIZM2K… | 1 | 2,273 | 2.20× |
| olmocr/old_scans_math/2_pg39 | 1 | 682 | 2.16× |
| soumu/n1110000（日文） | 2 | 1,150 | 1.99× |
| olmocr/headers_footers/0387… | 1 | 2,688 | 1.95× |
| olmocr/arxiv_math/2503.03762 | 1 | 2,873 | 1.89× |
| olmocr/arxiv_math/2503.03855 | 1 | 2,954 | 1.86× |
| olmocr/tables/1529… | 1 | 3,044 | 1.84× |
| olmocr/multi_column/03cc… | 1 | 3,192 | 1.80× |
| olmocr/arxiv_math/2503.02004 | 1 | 3,719 | 1.73× |
| olmocr/multi_column/0083… | 1 | 5,270 | 1.48× |
| **合計（16 份）** | | | **2.25×** |

**poppler 路徑（>10 頁）。** Claude Code 執行 `pdftoppm -jpeg -r 100`，只送圖片，所以在這裡文字層有可能比圖片更貴。

| 檔案 | 頁數 | 每頁字元 | 圖片 token | 文字 token | 比值 |
|---|---:|---:|---:|---:|---:|
| zenodo/22072701（投影片） | 32 | 250 | 41,472 | 2,003 | 20.70× |
| zenodo/22069734（投影片） | 26 | 330 | 32,240 | 2,149 | 15.00× |
| zenodo/22070313（投影片） | 16 | 372 | 19,840 | 1,492 | 13.30× |
| zenodo/22070195（投影片） | 21 | 449 | 26,040 | 2,359 | 11.04× |
| govinfo/BILLS-117hr3684enr | 1,039 | 2,729 | 2,019,816 | 709,308 | 2.85× |
| govinfo/BILLS-117hr5376enr | 273 | 2,742 | 530,712 | 187,211 | 2.83× |
| ndc/population-projection-2024（中文） | 110 | 1,515 | 138,600 | 52,720 | 2.63× |
| soumu/n1210000（日文） | 14 | 863 | 17,640 | 7,479 | 2.36× |
| soumu/n2110000（日文） | 11 | 1,203 | 13,860 | 6,610 | 2.10× |
| loc/IMJ4MPPY…（dot-gov） | 143 | 3,545 | 177,320 | 126,741 | 1.40× |
| loc/QSPE3BHA…（dot-gov） | 83 | 8,346 | 102,920 | 173,187 | **0.59×** |
| **合計（11 份）** | | | 3,120,460 | 1,271,259 | **2.45×** |

poppler 路徑的損益兩平點：一頁 letter 尺寸在 100 DPI 下約 1,240 個圖片 token，所以只有當一頁超過約 5,000 個拉丁字元或約 1,600 個 CJK 字元時，文字層才會比較貴。

依類別，合計 27 份有文字層的檔案：

| 類別 | 整體比值 |
|---|---|
| 投影片（5，Zenodo CC-BY） | **10.59×** |
| 法規（2，govinfo） | 2.84× |
| 中日文白皮書與報告（4，総務省＋國發會） | 2.53× |
| 其他（3：頁首頁尾頁 2 份、有文字層的舊數學掃描 1 份） | 2.26× |
| 密集單頁（6，arXiv／多欄／表格） | 1.74× |
| 政府文件（7，LoC dot-gov） | 0.95×——七份中有六份落在 1.40× 到 3.56× 之間（合計 1.43×）；第七份 `loc/QSPE3BHA…` 是 83 頁、每頁 8,346 字元，得到 0.59×，把整組拖到 1 以下 |
| 全部 27 份有文字層的檔案 | 2.45×（最佳 20.70×、中位數 2.36×、最差 0.59×） |

每頁少於 1,500 字元的稀疏頁平均 6.58×，密集頁 1.96×。

**估算的前提。** 兩欄 token 都是估算，不是帳單。

- 文字：拉丁文字每 4 個字元一個 token，CJK 每 1.3 個字元一個 token，以去除控制字元後的抽取文字計算。
- 圖片：[官方文件的 vision 公式](https://docs.anthropic.com/en/docs/build-with-claude/vision)，每張 `ceil(width/28) × ceil(height/28)` 個 token，長邊以 2,576 px 為上限（API 會縮圖的地方），單張圖片最多計 4,784 個 token。benchmark 直接呼叫 hook 自己的 `est_image_tokens()`，所以表格與 hook 告訴 Claude 的數字不會分岔。
- poppler 路徑的 100 DPI 是 Claude Code 傳給 `pdftoppm` 的參數（`-r 100`，見 [#23704](https://github.com/anthropics/claude-code/issues/23704) 的回報）。
- document block 路徑的渲染解析度未公開。表格用頁面自身的點數尺寸（72 DPI）當下限；Anthropic 自己給的[每頁 1,500–3,000 token](https://docs.anthropic.com/en/docs/build-with-claude/pdf-support) 當上限。72 DPI 下一頁 letter 約 640 個圖片 token 加上文字——低於 Anthropic 區間的下緣，所以上表的 document block 比值只會低估、不會高估。
- 「Claude Code 對 ≤10 頁的檔案用 document block 而不是自己渲染」是從 #23704 裡 collaborator 的留言推論的，不是從原始碼確認的。
- 頁面尺寸只取第一頁，套用到全部頁面。
- 「掃描件」與「文字」的判定就是 hook 自己的判定（`classify()`），所以表裡叫掃描件的檔案，就是 hook 會送去 vision 的檔案。

投影片的代價換成數字：20.70× 的最佳案例是一份 32 頁的投影片，其中 23 頁不到 200 個字元——地圖上的測站名與比例尺不在文字層裡；`zenodo/22070313` 第 9 頁的文字層只剩頁碼，其他全部燒在圖裡。hook 把第 9 頁標成 image 頁、第 8 頁（86 個字元、45% 影像）標成 figure 頁；在那份 32 頁的投影片裡標出 10 頁 figure 頁，第 28 頁那張用向量路徑畫的地圖則標不出來。

重跑：

```
python scripts/fetch_corpus.py                                   # 那 35 份檔案，附 sha256 驗證
python scripts/benchmark.py corpus --manifest scripts/corpus.json --csv scripts/results/today.csv
python scripts/benchmark.py ~/docs --anonymize                   # 你自己的檔案，輸出可安全貼進 issue
```

## 為什麼有這個東西

這個問題不是我發現的。[`anthropics/claude-code#23704`](https://github.com/anthropics/claude-code/issues/23704)——標題是「Read tool's PDF support requires poppler-utils but it's undocumented, usually absent, and not detected after install」——自 2026 年 2 月開啟至今仍是 OPEN，20 個 👍，Linux、macOS、Windows 三個平台都有回報。2026-08-17 一位 Anthropic collaborator 確認了它的形狀：

> 十頁以內的 PDF 不需要 poppler；只有長文件的分頁讀取需要 poppler-utils 的 `pdftoppm`／`pdfinfo`。**文件至今仍未提及這項需求，所以我們把這條留著當文件待辦。**

原始回報者自己提出的 workaround，正是這支 hook 自動化的東西：「the workaround is `curl` + `pdftotext` + `Read` on the resulting text file — 3 tool calls for what should be a single `Read`」。

底下還有第二個問題。[#23699](https://github.com/anthropics/claude-code/issues/23699)——Read 的錯誤不會顯示在 TUI——**已被關閉並標記 stale**。所以這件事發生時，你看不到 `pdftoppm is not installed`，你看到的是 Claude 讀不到 PDF，然後開始亂猜。

**如果 Anthropic 替 Read 加上 text-first 模式，請把這支解除安裝。** 那才是該期待的結果；#23704 就是推動它的地方。

## 設計說明

**引擎。** 預設是 `pypdfium2`（BSD-3-Clause／Apache-2.0）。`pymupdf` 若已安裝就會被使用，但永遠不會是相依項——它是 AGPL-3.0，而那恰好會擋住最在意 token 成本的那群人。`pypdf` 是最後手段：它無法渲染，而且在複雜版面上抽出的結果與 `pypdfium2` 不一致——在語料裡四份 CJK 檔案上，它的 CJK 字元數與 `pypdfium2` 相差 −0.8% 到 +17%（`pypdf` 6.13），所以用其中一個引擎做出的判定或快取，不能直接當成另一個的。渲染不會拉進任何第三方影像函式庫：一張 PNG 不過是一組簽章、三個 chunk 和一段 zlib 資料流，所以 hook 直接用 `zlib` 與 `struct` 把 pdfium 的像素緩衝區寫出去（MuPDF 本來就不需要，`fitz.Pixmap.save()` 自己會寫 PNG）。跟它取代掉的 `pillow` 路徑實測比較，這條路快 22%，但檔案大 42%——像素完全相同，差別在於每一列都用 filter 0，而 `pillow` 會逐列挑一個 filter。這個大小差異只花到本機快取的磁碟空間，讓 500 MB 的清理門檻更早被觸發；沒有任何一個位元組會進到模型那邊。壓得更用力並不划算：zlib level 9 只再省 10%，卻要花三倍時間，所以寫入端維持 level 6。

**缺引擎時。** 以前一個引擎都裝不到時，hook 會載入、什麼都不決定、每一次 Read 都放行——從外面看跟「根本沒裝」完全一樣，一份裝好的東西就這樣可以空轉好幾個月而看起來一切正常。現在它仍然放行，但會附上一則說明，告訴模型 pdf-text-router 有裝但是空轉、`pip install pypdfium2` 才是解法，而且這一次的 Read 就照沒有 hook 時的樣子繼續走。只裝了 `pypdf`、而這次又非渲染不可時也一樣——抽文字可以，掃描件不行。兩則說明都由快取目錄裡的 `.install-notice` 標記節流成每 7 天最多一次；如果那個目錄寫不進去，就什麼都不說，因為每一次 Read 都印一則比沉默更糟。單純解析失敗的檔案維持沉默——PDF 壞掉不等於少裝套件，講錯了只會讓你去找一個並不存在的解法。已知的小毛病：`--clear-cache` 與自動清理會連 `.install-notice` 一起刪掉，那 7 天就重新起算；最壞的情況是多印一次。

**快取。** `~/.claude/pdf-text-cache`，可用 `PDF_TEXT_ROUTER_CACHE` 覆寫。每一筆都是明文：`<key>-<name>.txt` 是 Claude 讀的檔案；旁邊的 `<key>-<name>.json` 是 sidecar（頁數、每頁清理前後的字元數、每頁影像面積比、頁面尺寸、引擎、token 估計值、每個頁碼標記所在的行號、以及 key），有它在，快取命中時可以完全不開 PDF 就做出決定；`<key>-p<N>-1.5x-<name>.png` 是渲染出來的頁面。舊版寫出的 sidecar 缺 `page_image_area`、`page_raw_chars` 或 `key` 其中一個欄位，就不採信，那份檔案會重抽一次。key 是檔案的大小、mtime 加上頭尾各 256 KB 內容的雜湊，而不是只看 `stat()`：重新匯出一份 PDF 常常落在同一秒、同樣的位元組數，只以 stat 為 key 的快取這時會端出上一版的文字，而本來會抓到這件事的那次 Read 已經被改寫掉了。每一筆都先寫進暫存檔再改名，而且 sidecar 只有在旁邊有一份完整的 `.txt` 時才被採信，因為一份被截斷的抽取比沒有抽取更糟。清理在每一次寫入時執行：先刪掉超過 30 天的項目，再從最舊的開始刪到目錄低於 500 MB（`PDF_TEXT_ROUTER_CACHE_MAX_MB`）為止，剛寫入的檔案除外。年齡從寫入那一刻起算——快取命中不會更新它——所以一筆項目在抽取後 30 天到期，不管你昨天有沒有讀過。`--clear-cache` 可以隨時清空整個目錄。

關於隱私：PDF 本身就是這台機器上一份讀得到的檔案，所以快取多出來的暴露，只發生在 PDF 放在家目錄以外的地方——加密磁碟、外接碟、網路位置——的時候。`PDF_TEXT_ROUTER_CACHE=beside` 改成把快取寫在 PDF 旁邊：`report.pdf.txt`、`report.pdf.json`、`report.pdf.p3.1.5x.png`，檔名裡沒有 key（key 存在 sidecar 裡，每次命中都會核對；比 PDF 舊的 PNG 會重新渲染）。PDF 所在的資料夾不可寫時，那份檔案退回集中目錄，並在 stderr 印一行。放在 PDF 旁邊的東西永遠不會被清理——那是你的資料夾，不是 hook 的——而 beside 模式下的 `--clear-cache` 什麼都不刪，只印出檔案在哪裡。仍然沒有加密模式。

**模式。** `PDF_TEXT_ROUTER_MODE=rewrite`（預設）用 hook 的 `updatedInput` 把 Read 指向 `.txt`（或第一張 `.png`），只花一次工具呼叫；其他任何值都當成 `rewrite`。`PDF_TEXT_ROUTER_MODE=deny` 是給還不支援 `updatedInput` 的舊版 Claude Code 的退路：deny 那次 Read、把 `.txt` 或 `.png` 路徑寫在理由裡，Claude 下一次呼叫再去讀。頁碼超出範圍與 `pages` 解析不了這兩種情況在兩種模式下都是 deny——沒有東西可以轉向。

**環境變數。** `PDF_TEXT_ROUTER_MODE`（`rewrite`｜`deny`）、`PDF_TEXT_ROUTER_CACHE`（一個目錄，或寫 `beside` 讓快取放在每份 PDF 旁邊；見「快取」）、`PDF_TEXT_ROUTER_CACHE_MAX_MB`（預設 500；只管集中目錄）、`PDF_TEXT_ROUTER_SCALE`（本機渲染 PNG 的比例，1.0–3.0，預設 1.5 ≈ 108 DPI；超出範圍的值會被夾回區間內）。

**逾時。** 60 秒，設定在 `hooks/hooks.json`。超時的 hook 會被砍掉、等於沒有做出決定，所以一份病態的 PDF 會退化成原生路徑，而不是一次被擋住的 Read；寫到一半的快取項目永遠不會被採信，因為 `.txt` 只在完整時才改名就位，sidecar 又在它之後才寫。那份 1,039 頁的法案第一次抽取要 2.1–2.3 秒；有快取之後第二次 0.3 秒，而且不會打開 PDF。

**平台狀態。** hook 在 Claude Code 裡的行為只在 Windows 上實機驗證過（Claude Code 2.1.241）。單元測試在 CI 上跑 Linux、macOS、Windows 三個平台；macOS 與 Linux 上端到端的 hook 路徑我沒有跑過——如果你在那裡用了，一則寫「能用」的 issue 和一則寫「不能用」的一樣有價值。

**與官方 PDF skill 的關係。** 見上方的[與官方 PDF skill 的分工](#與官方-pdf-skill-的分工)。

## 測試

```
python -m pytest tests -q
```

單元測試不需要任何樣本檔——fixture 是在記憶體裡組出來的最小 PDF，xref 表的位移是算出來的。`tests/test_corpus.py` 另外對那 35 份 benchmark 檔案做七項檢查（每一份人工核對過的掃描件都被判成掃描件、其餘都是文字、國發會報告能點名它的空白頁與整頁圖、已知頁面得到該有的分類、向量地圖是接受的漏抓、路由的 dry-run 與判定一致），`corpus/` 沒抓下來時整個模組跳過：沒語料是 182 passed，有語料是 189 passed。其中有幾個測試的存在理由是：舊版測試在它宣稱要測的東西壞掉時仍然全過，那幾個測試裡都寫明了這件事。同一套測試加上 `--selftest`，每次 push 都會在三個作業系統、兩個 Python 版本（3.9 與 3.12）上跑（[workflow](.github/workflows/test.yml)）。

## 授權

MIT
