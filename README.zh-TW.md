# pdf-text-router

一支 Claude Code hook，把每一次對 PDF 的 `Read` 導向真正走得通的那條路。

[English](README.md) · 繁體中文

Claude Code 讀 PDF 的方式是把每一頁渲染成圖片。對掃描件而言這是對的——vision 本來就是 OCR 引擎；但對有文字層的檔案，那等於花錢買一張「文字的照片」。而超過十頁的 PDF 還會呼叫 poppler 的 `pdftoppm`，那是一支官方安裝文件從未提及的外部程式。

這支 hook 攔下那次 `Read`，改走另一條路。

## 這個問題不是我發現的

[`anthropics/claude-code#23704`](https://github.com/anthropics/claude-code/issues/23704)——標題是「Read tool's PDF support requires poppler-utils but it's undocumented, usually absent, and not detected after install」——自 2026 年 2 月開啟至今仍是 OPEN，20 個 👍，Linux、macOS、Windows 三個平台都有回報。2026-08-17 一位 Anthropic collaborator 確認了它的形狀：

> 十頁以內的 PDF 不需要 poppler；只有長文件的分頁讀取需要 poppler-utils 的 `pdftoppm`／`pdfinfo`。**文件至今仍未提及這項需求，所以我們把這條留著當文件待辦。**

原始回報者自己提出的 workaround，正是這支 hook 自動化的東西：「the workaround is `curl` + `pdftotext` + `Read` on the resulting text file — 3 tool calls for what should be a single `Read`」。

底下還有第二個問題。[#23699](https://github.com/anthropics/claude-code/issues/23699)——Read 的錯誤不會顯示在 TUI——**已被關閉並標記 stale**。所以這件事發生時，你看不到 `pdftoppm is not installed`，你看到的是 Claude 讀不到 PDF，然後開始亂猜。

## 它做什麼

| 情境 | 走法 |
|---|---|
| 有文字層 | deny 這次 Read，抽成 UTF-8 `.txt`，把路徑交給 Claude |
| 掃描件，原生渲染可用 | 放行——掃描件本來就該交給 vision |
| 掃描件，沒有 poppler | deny，在本機把頁面渲成 PNG，交給 Claude |
| 明確的短頁碼範圍（≤3 頁） | 你是想「看」版面：放行；沒有 poppler 就本機渲染 |
| 頁碼超出文件範圍 | 直接講明，而不是讓 Claude 撞上一個與此無關的 poppler 錯誤 |
| 任何意外狀況 | 放行——hook 絕不該成為 Read 失敗的理由 |

抽出的文字帶著 `[page N/M]` 標記，所以 Claude 仍然引用得了頁碼。如果抽出的內容長到 Read 一次顯示不完，hook 會講明並要求改用 `Grep` 或 `offset`／`limit`，而不是默默交出一段開頭。

## 安裝

```
/plugin marketplace add WatsonTsai/pdf-text-router
/plugin install pdf-text-router@pdf-text-router
pip install pypdfium2 pillow
```

裝完務必確認它真的生效——這件事很重要，因為一支 fail-open 的 hook「沒生效」和「這份剛好是掃描件」在外觀上完全一樣：

```
python ~/.claude/plugins/.../hooks/pdf_text_router.py --selftest
```

它會回報使用中的引擎、渲染是否可用、`pdftoppm` 在不在 PATH 上。`--check FILE.pdf` 則會印出單一檔案的路由決策，不產生任何副作用。

<details>
<summary><b>手動安裝</b></summary>

<br>

把 `hooks/pdf_text_router.py` 複製到 `~/.claude/hooks/`，再把 `examples/settings.snippet.windows.json`（或 `.unix.json`）合併進 `~/.claude/settings.json`，並把 `YOUR_USERNAME` 換成實際路徑。`command` 欄位寫的是 `python`；如果你的 PATH 解析結果不是它，改成 `python3` 或直譯器的絕對路徑。

</details>

### 引擎

預設是 `pypdfium2`（BSD-3-Clause／Apache-2.0）。`pymupdf` 若已安裝就會被使用，但永遠不會是相依項——它是 AGPL-3.0，而那恰好會擋住最在意 token 成本的那群人。`pypdf` 是最後手段：它無法渲染，而且在一份雙欄報告上掉了 7.7% 的中文字，那些字另外兩個引擎都抽得出來。

`pillow` 只有渲染路徑會用到。如果你不會在沒有 poppler 的情況下讀掃描件，就完全不需要它。

## 它在哪裡有用，在哪裡沒用

節省幅度與每頁字數密度成反比。在 [35 份公開 PDF](scripts/corpus.json) 上實測——全部免註冊可下載，授權為 ODC-BY、CC-BY 或美日政府條款：

| 類別 | 整體比值 |
|---|---|
| 投影片（5，Zenodo CC-BY） | **10.71×** |
| 法規（2，govinfo） | 2.93× |
| 中日文白皮書與報告（4，総務省＋國發會） | 2.59× |
| 密集單頁（6，arXiv／多欄／表格） | 1.74× |
| **政府文件（7，LoC dot-gov）** | **0.96×——這一組是淨虧損** |
| 全部 27 份有文字層的檔案 | 2.51× |

每頁少於 1500 字元的稀疏頁平均 6.64×，密集頁只有 1.98×。

**所以：如果你讀的大多是文字密集的文件，這支 hook 幫你省的很少，個別檔案上甚至會倒賠——那批語料裡最差的一份是 0.60×。** 它在那些檔案上仍然做到的事，是在缺少 poppler 時讓文件保持可讀，而十頁以上的差別就是「一份文件」與「一則錯誤訊息」。

在你自己的文件上重跑：

```
python scripts/fetch_corpus.py          # 那 35 份公開檔案，附 sha256 驗證
python scripts/benchmark.py corpus/     # 或指向你自己的資料夾
python scripts/benchmark.py ~/docs --anonymize   # 輸出可安全貼進 issue
```

兩欄 token 都是估算而非帳單：文字以 CJK 每 1.3 字元、其餘每 4 字元計為一個 token，圖片以 `(寬 × 高) / 750` 計，並依 Claude Code 對該檔案實際會走的路徑分別計算。

## 幾點說明

快取放在 `~/.claude/pdf-text-cache`（可用 `PDF_TEXT_ROUTER_CACHE` 覆寫）。它以檔案內容為 key，而不是 `stat()`——重新匯出一份 PDF 常常落在同一秒、同樣的位元組數，以 stat 為 key 的快取這時會端出上一版的文字，而真正的 Read 仍被 deny，沒有人看得出來。每一筆都先寫進暫存檔再改名，因為 Claude Code 會殺掉超時的 hook，而一份被截斷的抽取比沒有抽取更糟。快取沒有任何清理機制，想刪隨時可以整個刪掉。

這支 hook 與官方的 [`anthropics/skills` PDF skill](https://github.com/anthropics/skills/tree/main/skills/pdf) 互補。那一支管的是「對 PDF 做事」——合併、切分、填表單；這一支管的是「把 PDF 讀進 context」。

**如果 Anthropic 替 Read 加上 text-first 模式，請把這支解除安裝。** 那才是該期待的結果；#23704 就是推動它的地方。

## 測試

```
python -m unittest discover -s tests
```

59 個測試，不需要任何樣本檔——fixture 是在記憶體裡組出來的最小 PDF，xref 表的位移是算出來的。其中有幾個測試的存在理由是：舊版測試在它宣稱要測的東西壞掉時仍然全過。那幾個測試裡都寫明了這件事。

## 授權

MIT
