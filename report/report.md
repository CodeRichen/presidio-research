# 即時剪貼簿去識別化 / 還原工具 — 講解文件

## 1. 這個工具在解決什麼問題

當你需要把包含 API Key、密碼、電話、信用卡號等敏感資訊的文字（例如貼給 ChatGPT、貼到公開頻道、貼進文件）分享出去時，
很容易不小心外洩機密。這支腳本會**持續監控剪貼簿**：

- 你複製一段「原始文字」→ 腳本自動掃描，把偵測到的敏感內容換成 `<ENTITY_n>` 這種標籤，並存進對照表，然後把「去識別化後的文字」放回剪貼簿。
- 你複製一段「已經含有 `<ENTITY_n>` 標籤的文字」→ 腳本會直接查表，把標籤還原成原本的敏感內容，再放回剪貼簿。

也就是說，它同時扮演「遮蔽器」與「還原器」，靠**是否偵測到 `<TAG_n>` 格式**來自動判斷該做哪一件事。

---

## 2. 整體架構

```
剪貼簿內容
    │
    ▼
是否含有 <ENTITY_n> 標籤？
    │
   否 ─────────────► anonymize()：去識別化流程
    │                     │
   是                     ├─ 1) 分段 (CLASSIFY_MODE)
    │                     ├─ 2) 依段落類型跑對應過濾器
    ▼                     ├─ 3) 合併重疊比對結果
deanonymize()：查表還原      ├─ 4) 建立/查詢標籤，寫回 clipboard_map.txt
    │                     └─ 5) 用標籤取代原文，回填剪貼簿
    ▼
還原後文字回填剪貼簿
```

### 核心組成

| 模組                                    | 功能                                                            |
| --------------------------------------- | --------------------------------------------------------------- |
| `pyperclip`                           | 讀寫系統剪貼簿，主迴圈每 0.5 秒輪詢一次                         |
| Qwen2.5-Coder 分類模型（可選）          | 把貼上的內容切成`code` / `text` / `question` 段落         |
| 正則規則表`RULES`                     | 用來抓 API Key、JWT、電話、信用卡、URL、檔名、帳密風格字串等    |
| Microsoft Presidio (`AnalyzerEngine`) | 針對一般文字段落，用 NLP 模型抓姓名、地址等更難用正則涵蓋的個資 |
| `clipboard_map.txt`                   | 標籤 ↔ 原始內容的對照表（純文字，Tab 分隔）                    |

---

## 3. 三種分段模式：`CLASSIFY_MODE`

腳本開頭有一個全域變數 `CLASSIFY_MODE`，控制要不要呼叫 LLM 分段：

- **`"auto"`**：呼叫本地 `Qwen/Qwen2.5-Coder-1.5B-Instruct` 模型，把貼上的文字依語意切成多個段落，
  每段標記為 `code`、`text` 或 `question`。效果最準（程式碼段落只跑程式碼規則，文字段落才跑個資規則 + Presidio），
  但需要載入模型、跑起來較慢、吃記憶體。
- **`"code"`**：略過分類，整段文字都當作「程式碼」，只跑 `CODE_FILTERS`。
- **`"text"`**：略過分類，整段文字都當作「一般文字」，只跑 `TEXT_FILTERS`（正則 + Presidio）。目前程式碼中預設就是這個模式。

分類完成後，`locate_segments()` 會把模型回傳的「段落內容」重新對應回原文字串中的 `(start, end, type)` 座標，
方便後續用座標做取代，而不是直接用模型輸出的文字（避免因為模型微幅改寫內容而對不齊）。

---

## 4. 正則規則表：`RULES`

所有正則規則都定義成 `Rule` 這個 dataclass，統一管理，方便新增/刪除：

```python
@dataclass
class Rule:
    name: str                # 標籤類別名稱，例如 "JWT"、"TW_PHONE"
    pattern: str              # 正則表達式
    flags: int = 0
    validator: Callable = None   # 二次驗證函式，降低誤報
    category: str = "text"       # "code" 或 "text"
    code_only: bool = False      # True 代表只在程式碼段落套用
```

**目前涵蓋的規則類型：**

- **只在程式碼段落用**：`FILE_PATH`（Windows / Unix 路徑）
- **程式碼與文字都會跑**：`JWT`、各家 `API_KEY_*`（OpenAI / AWS / GitHub / Google）
- **個資 / 憑證類（文字段落主力，也會併入程式碼段落一起跑）**：
  - `TW_PHONE`：台灣手機號碼
  - `CREDIT_CARD`：抓到數字序列後，用 **Luhn 演算法**（`luhn_valid()`）二次驗證，避免把一般長數字誤判成信用卡號
  - `URL`：整個網址一起遮，順便蓋掉網址裡夾帶的帳密或 token
  - `FILE_NAME`：依副檔名清單（`FILE_EXTENSIONS`）比對檔名
  - `CREDENTIAL_LIKE`：8~29 字、同時有英文字母與數字的字串（沒加引號時門檻較嚴格）
  - `GENERIC_SECRET`：30 字以上、同時含字母數字的長亂碼（例如各種 access/secret key）
  - `QUOTED_SECRET`：被各種引號包住的內容，用 `quoted_sensitive_valid()` 驗證是否像網址／檔名／密碼樣式（引號內門檻可以放寬到 4 字）

**為什麼要用 `validator`？** 正則本身容易「過度比對」（例如任何 13～19 位數字都會被 `CREDIT_CARD` 抓到），
所以額外用一個函式做語意層級的二次確認，減少誤判。

**要新增自己的規則**，只要照格式在 `RULES` 這個 list 裡加一行，例如：

```python
Rule("TW_ID", r"\b[A-Z][12]\d{8}\b", validator=tw_id_valid, category="text"),
```

---

## 5. 重疊比對的合併：`merge_overlaps()`

因為同一段文字可能被多個規則、甚至正則 + Presidio 同時比對到（例如一個 URL 裡面剛好也符合 `CREDENTIAL_LIKE`），
若不處理直接取代會把字串切壞。`merge_overlaps()` 的做法：

1. 依「起始位置」排序，起始位置相同時**長度較長者優先**。
2. 由左到右掃描，只保留與前一個保留區間不重疊的比對結果。

這樣可以確保最後選出的區間彼此不重疊，且優先保留「範圍較大、資訊量較完整」的那個標籤。

---

## 6. 標籤系統與對照表

- 對照表存在 `clipboard_map.txt`，格式是 `標籤\t原始內容`，每行一筆。
- 同一個原始內容第二次出現時，會直接複用既有標籤（透過 `reverse = {原始內容: 標籤}` 反查），不會重複建立新標籤。
- 每個實體類型（`etype`）各自有自己的流水號計數器（`counters`），例如 `<API_KEY_OPENAI_1>`、`<API_KEY_OPENAI_2>`，
  程式啟動時會先讀取既有對照表，把計數器接續上去，避免標籤重複或覆蓋。
- 還原時（`deanonymize()`）用 `TAG_RE = r"<([A-Z_]+)_(\d+)>"` 這個正則抓出文字中所有標籤，逐一查表換回原文；
  對照表中查不到的標籤會原樣保留（不會噴錯）。

---

## 7. 主迴圈流程 `main()`

```python
while True:
    text = pyperclip.paste()
    if text and text != last:
        if TAG_RE.search(text):
            new_text = deanonymize(text, mapping)   # 含標籤 -> 還原
        else:
            new_text = anonymize(text, mapping)      # 不含標籤 -> 去識別化
        if new_text and new_text != text:
            pyperclip.copy(new_text)
        last = text
    time.sleep(0.5)
```

- 用 `last` 變數避免同一段文字被重複處理（防止自己覆寫剪貼簿又觸發自己的迴圈）。
- 每 0.5 秒輪詢一次剪貼簿，屬於輕量級的 polling，不需要作業系統層級的剪貼簿事件監聽。
- 若 `anonymize()` 掃描後沒找到任何敏感資訊（`matches` 為空），回傳 `None`，剪貼簿內容維持不變。

---

## 8. 安裝與執行

```bash
pip install presidio-analyzer presidio-anonymizer pyperclip pywin32 transformers torch accelerate
python -m spacy download en_core_web_lg
python clipboard_tool.py
```

- 若 `CLASSIFY_MODE = "auto"`，首次執行會自動從 Hugging Face 下載 `Qwen/Qwen2.5-Coder-1.5B-Instruct`。
- `pywin32` 是給 Windows 剪貼簿存取用的相依套件（`pyperclip` 在 Windows 上會用到）。

---

## 9. 使用情境範例

**去識別化：**

1. 複製一段含 API Key 的文字，例如：
   `我的 key 是 sk-proj-abcdef1234567890abcdef1234567890`
2. 腳本偵測到符合 `API_KEY_OPENAI` 規則，自動改成：
   `我的 key 是 <API_KEY_OPENAI_1>`
3. 對照表寫入一筆：`<API_KEY_OPENAI_1>\tsk-proj-abcdef1234567890abcdef1234567890`
4. 這段「已去識別化」的文字可以安心貼給別人或貼到 AI 工具。

**還原：**

1. 複製含 `<API_KEY_OPENAI_1>` 的文字（例如從別人回傳的內容再貼回來）。
2. 腳本偵測到標籤格式，直接查 `clipboard_map.txt`，換回原始 key，貼回剪貼簿。

---

## 10. 已知限制與注意事項

- `clipboard_map.txt` 是**明文**存放原始敏感資訊，等於把所有機密集中在一個檔案裡，需自行做好檔案權限與加密／備份控管。
- 正則規則無法涵蓋所有敏感資訊型態（例如非台灣格式的電話、其他國家的身分證字號），需要依實際使用情境擴充 `RULES`。
- `CLASSIFY_MODE = "auto"` 依賴 LLM 輸出合法 JSON，若模型格式跑掉，`classify_segments()` 會 fallback 成「整段當一般文字」，
  不會因為解析失敗而漏掃，但也可能失去「程式碼段落跳過個資規則」的效率優化。
- Presidio 的 `analyzer.analyze(..., language="en")` 目前寫死英文語言模型，若貼上的是中文個資（如中文姓名），
  預設的 `en_core_web_lg` 模型辨識效果有限，需要額外安裝並切換中文語言模型才能提升準確率。
