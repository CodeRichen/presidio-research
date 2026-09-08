"""
圖片 / PDF 去識別化(遮蔽) + 還原工具
====================================
與 clipboard 版本的差異：clipboard 工具處理的是「純文字」，可以直接用 <TAG_n> 取代原文；
但圖片、PDF 的敏感資訊是「畫面上的一塊像素」，沒辦法用文字標籤取代，所以改用：

    1) OCR / PDF 文字座標找出敏感內容的位置 (bounding box)
    2) 塗黑該區域 (遮蔽)
    3) 塗黑前，先把該區域「原始畫面」裁切下來，存成 base64 圖片，寫進對照表 (media_deid_map.json)
    4) 還原時，依對照表把裁切下來的原始畫面貼回對應座標

處理兩種來源：
- 圖片檔 (jpg/png/...)：全部走 Tesseract OCR。
- PDF：
    - 有文字圖層的頁面：直接用 PyMuPDF 抓文字座標比對，用「真正的遮蔽 (redaction)」把文字內容整個
      移除，不是只疊一層黑框。
    - 幾乎沒有文字圖層的頁面 (掃描件/純圖片頁)：先整頁轉成圖片，走跟圖片檔一樣的 OCR 流程，
      找到的像素座標再換算回 PDF 座標，一樣用 redaction 塗黑。

安裝：
    pip install pillow pytesseract pymupdf presidio-analyzer
    python -m spacy download en_core_web_lg   # Presidio 用，不需要英文 NER 偵測可省略，程式會自動略過

    另外需要安裝 Tesseract OCR 主程式 (非 pip 套件，pytesseract 只是呼叫它的 Python 介面)：
        Windows: https://github.com/UB-Mannheim/tesseract/wiki (安裝時記得勾選 Chinese-Traditional)
        macOS  : brew install tesseract tesseract-lang
        Linux  : sudo apt install tesseract-ocr tesseract-ocr-chi-tra
    若 Windows 上找不到 tesseract 執行檔，需在程式最上面手動指定路徑，例如：
        pytesseract.pytesseract.tesseract_cmd = r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe"

用法範例：
    python image_pdf_deid_tool.py mask-image id_card.jpg id_card_masked.jpg
    python image_pdf_deid_tool.py restore-image id_card_masked.jpg id_card_restored.jpg
    python image_pdf_deid_tool.py mask-pdf contract.pdf contract_masked.pdf
    python image_pdf_deid_tool.py restore-pdf contract_masked.pdf contract_restored.pdf
"""

import os
import io
import re
import json
import base64
import argparse
from dataclasses import dataclass
from typing import Callable, Optional, List, Tuple

from PIL import Image, ImageDraw
import pytesseract
import fitz  # PyMuPDF

try:
    from presidio_analyzer import AnalyzerEngine
    _analyzer = AnalyzerEngine()
except Exception:
    _analyzer = None  # 沒裝 presidio 或對應模型時，自動退化成只用下面的正則規則

OCR_LANG = "chi_tra+eng"      # Tesseract 語言包，可依需求調整，例如只用 "eng"
MASK_COLOR = (0, 0, 0)        # 遮蔽色塊顏色 (黑色)
MASK_PADDING = 2              # 遮蔽框比偵測到的文字框多留幾個 px，避免邊緣殘留
DEFAULT_MAP_FILE = "media_deid_map.json"


# ========== 正則規則：偵測邏輯與 clipboard 工具同一套精神，這裡聚焦在圖片/PDF 常見的敏感資訊類型 ==========
def luhn_valid(value: str) -> bool:
    """信用卡卡號 Luhn 演算法驗證，降低把一般長數字誤判成卡號的機率"""
    digits = [int(d) for d in value if d.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    checksum, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


@dataclass
class Rule:
    name: str
    pattern: str
    flags: int = 0
    validator: Optional[Callable[[str], bool]] = None


RULES = [
    Rule("EMAIL", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    Rule("TW_PHONE", r"\b09\d{2}[- ]?\d{3}[- ]?\d{3}\b"),
    Rule("TW_ID", r"\b[A-Z][12]\d{8}\b"),
    Rule("CREDIT_CARD", r"\b(?:\d[ -]?){13,19}\b", validator=luhn_valid),
    Rule("JWT", r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    Rule("API_KEY_OPENAI", r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b"),
    Rule("API_KEY_AWS", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    Rule("API_KEY_GITHUB", r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"),
    Rule("API_KEY_GOOGLE", r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    Rule("URL", r"\bhttps?://[^\s\"'<>]+"),
    # 中長度帳密風格字串 (門檻刻意保守，避免圖片上一般英數混排文字被大量誤判)
    Rule("CREDENTIAL_LIKE", r"\b(?=[A-Za-z0-9]{8,29}\b)(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8,29}\b"),
    Rule("GENERIC_SECRET", r"\b(?=[A-Za-z0-9_-]{30,}\b)(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{30,}\b"),
    # 想加規則，照這個格式在這裡新增一行即可，例如：
    # Rule("PASSPORT_TW", r"\b\d{9}\b"),
]
_COMPILED = [(r.name, re.compile(r.pattern, r.flags), r.validator) for r in RULES]


def _merge_overlaps(matches):
    """多個規則/模型比對到重疊區間時，只保留較長、彼此不重疊的那一個"""
    matches = sorted(matches, key=lambda m: (m[0], -(m[1] - m[0])))
    merged, last_end = [], -1
    for start, end, label in matches:
        if start >= last_end:
            merged.append((start, end, label))
            last_end = end
    return merged


def detect_matches(text: str) -> List[Tuple[int, int, str]]:
    """在一段文字裡用正則 (+ Presidio，若有安裝) 找出敏感資訊的 (start, end, label)"""
    matches = []
    for name, pattern, validator in _COMPILED:
        for m in pattern.finditer(text):
            value = m.group(0)
            if validator is None or validator(value):
                matches.append((m.start(), m.end(), name))
    if _analyzer is not None:
        try:
            for r in _analyzer.analyze(text=text, language="en"):
                matches.append((r.start, r.end, r.entity_type))
        except Exception:
            pass  # Presidio 分析失敗不影響正則規則的結果
    return _merge_overlaps(matches)


# ========== OCR 共用工具：圖片檔、PDF 掃描頁都會用到 ==========
def ocr_words(pil_image: Image.Image) -> List[dict]:
    """回傳 pytesseract 逐字辨識結果，過濾掉空白與無效信心值的項目"""
    data = pytesseract.image_to_data(pil_image, lang=OCR_LANG, output_type=pytesseract.Output.DICT)
    words = []
    for i in range(len(data["text"])):
        txt = data["text"][i].strip()
        try:
            conf = int(float(data["conf"][i]))
        except (ValueError, TypeError):
            conf = -1
        if not txt or conf < 0:
            continue
        words.append({
            "text": txt,
            "left": data["left"][i], "top": data["top"][i],
            "width": data["width"][i], "height": data["height"][i],
            "block": data["block_num"][i], "par": data["par_num"][i],
            "line": data["line_num"][i], "word_num": data["word_num"][i],
        })
    return words


def _group_lines(words: List[dict]) -> List[List[dict]]:
    """把同一行 (block/par/line 相同) 的字組成一組，依 word_num 排序，還原成閱讀順序"""
    lines = {}
    for w in words:
        key = (w["block"], w["par"], w["line"])
        lines.setdefault(key, []).append(w)
    return [sorted(v, key=lambda w: w["word_num"]) for v in lines.values()]


def find_sensitive_boxes_in_words(words: List[dict]) -> List[Tuple[int, int, int, int, str]]:
    """把每一行組成文字、跑 detect_matches()，再把命中範圍換算回涵蓋這些字的像素框
    回傳 [(x0, y0, x1, y1, label), ...]（像素座標）"""
    boxes = []
    for line_words in _group_lines(words):
        line_text, offsets = "", []
        for w in line_words:
            start = len(line_text)
            line_text += w["text"]
            offsets.append((start, len(line_text), w))
            line_text += " "
        for start, end, label in detect_matches(line_text):
            hit_words = [w for s, e, w in offsets if s < end and e > start]
            if not hit_words:
                continue
            x0 = min(w["left"] for w in hit_words)
            y0 = min(w["top"] for w in hit_words)
            x1 = max(w["left"] + w["width"] for w in hit_words)
            y1 = max(w["top"] + w["height"] for w in hit_words)
            boxes.append((x0, y0, x1, y1, label))
    return boxes


# ========== 對照表 (media_deid_map.json) 讀寫：key 是「輸出檔案的檔名」 ==========
def _load_all_map(map_file: str) -> dict:
    if not os.path.exists(map_file):
        return {}
    with open(map_file, encoding="utf-8") as f:
        return json.load(f)


def _save_map_entry(map_file: str, key: str, entries: list):
    data = _load_all_map(map_file)
    data[key] = entries
    with open(map_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_map_entry(map_file: str, key: str) -> list:
    return _load_all_map(map_file).get(key, [])


# ========== 圖片檔遮蔽 / 還原 ==========
def _crop_to_png_b64(pil_image: Image.Image, box) -> str:
    crop = pil_image.crop(box)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def mask_image(input_path: str, output_path: str, map_file: str = DEFAULT_MAP_FILE):
    img = Image.open(input_path).convert("RGB")
    boxes = find_sensitive_boxes_in_words(ocr_words(img))

    entries, counters = [], {}
    draw = ImageDraw.Draw(img)
    for x0, y0, x1, y1, label in boxes:
        box = (x0 - MASK_PADDING, y0 - MASK_PADDING, x1 + MASK_PADDING, y1 + MASK_PADDING)
        counters[label] = counters.get(label, 0) + 1
        entries.append({
            "tag": f"{label}_{counters[label]}",
            "bbox": list(box),
            "crop_b64": _crop_to_png_b64(img, box),
        })
        draw.rectangle(box, fill=MASK_COLOR)

    img.save(output_path)
    _save_map_entry(map_file, os.path.basename(output_path), entries)
    print(f"[圖片遮蔽完成] {input_path} -> {output_path}，共遮蔽 {len(entries)} 處")
    return entries


def restore_image(masked_path: str, output_path: str, map_file: str = DEFAULT_MAP_FILE):
    entries = _load_map_entry(map_file, os.path.basename(masked_path))
    if not entries:
        print("[找不到對照資料] 無法還原，請確認 --map 路徑與檔名是否正確")
        return
    img = Image.open(masked_path).convert("RGB")
    for e in entries:
        crop = Image.open(io.BytesIO(base64.b64decode(e["crop_b64"])))
        x0, y0, _, _ = e["bbox"]
        img.paste(crop, (int(x0), int(y0)))
    img.save(output_path)
    print(f"[圖片還原完成] {masked_path} -> {output_path}，共還原 {len(entries)} 處")


# ========== PDF 遮蔽 / 還原 ==========
def _page_has_text(page: "fitz.Page", min_words: int = 5) -> bool:
    """粗略判斷這頁是「有文字圖層」還是「掃描/圖片型」頁面"""
    return len(page.get_text("words")) >= min_words


def _mask_pdf_text_page(page: "fitz.Page", entries: list, counters: dict):
    """有文字圖層的頁面：直接用 PyMuPDF 抓文字座標比對敏感資訊，用 redaction 真正移除文字"""
    words = page.get_text("words")  # (x0, y0, x1, y1, text, block_no, line_no, word_no)
    lines = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)

    for line_words in lines.values():
        line_words.sort(key=lambda w: w[7])
        line_text, offsets = "", []
        for w in line_words:
            start = len(line_text)
            line_text += w[4]
            offsets.append((start, len(line_text), w))
            line_text += " "

        for start, end, label in detect_matches(line_text):
            hit = [w for s, e, w in offsets if s < end and e > start]
            if not hit:
                continue
            x0 = min(w[0] for w in hit); y0 = min(w[1] for w in hit)
            x1 = max(w[2] for w in hit); y1 = max(w[3] for w in hit)
            rect = fitz.Rect(x0, y0, x1, y1)

            counters[label] = counters.get(label, 0) + 1
            # 塗黑前先把這塊區域的原始畫面存下來，供還原使用（annots=False 避免拍到還沒套用的遮蔽框）
            crop_pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(2, 2), annots=False)
            entries.append({
                "tag": f"{label}_{counters[label]}",
                "page": page.number,
                "bbox": [x0, y0, x1, y1],
                "value": " ".join(w[4] for w in hit),
                "crop_b64": base64.b64encode(crop_pix.tobytes("png")).decode("ascii"),
            })
            page.add_redact_annot(rect, fill=MASK_COLOR)
    page.apply_redactions()


def _mask_pdf_scanned_page(page: "fitz.Page", entries: list, counters: dict, zoom: float = 2.0):
    """幾乎沒有文字圖層的頁面 (掃描件)：整頁轉成圖片後走跟圖片檔一樣的 OCR 流程"""
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, annots=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    boxes = find_sensitive_boxes_in_words(ocr_words(img))

    for x0, y0, x1, y1, label in boxes:
        px0, py0 = x0 - MASK_PADDING, y0 - MASK_PADDING
        px1, py1 = x1 + MASK_PADDING, y1 + MASK_PADDING
        # 像素座標 / zoom 換算回 PDF 座標
        rect = fitz.Rect(px0 / zoom, py0 / zoom, px1 / zoom, py1 / zoom)

        counters[label] = counters.get(label, 0) + 1
        crop = img.crop((max(px0, 0), max(py0, 0), px1, py1))
        buf = io.BytesIO(); crop.save(buf, format="PNG")
        entries.append({
            "tag": f"{label}_{counters[label]}",
            "page": page.number,
            "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
            "crop_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
        })
        page.add_redact_annot(rect, fill=MASK_COLOR)
    page.apply_redactions()


def mask_pdf(input_path: str, output_path: str, map_file: str = DEFAULT_MAP_FILE):
    doc = fitz.open(input_path)
    entries, counters = [], {}
    for page in doc:
        if _page_has_text(page):
            _mask_pdf_text_page(page, entries, counters)
        else:
            _mask_pdf_scanned_page(page, entries, counters)
    doc.save(output_path)
    doc.close()
    _save_map_entry(map_file, os.path.basename(output_path), entries)
    print(f"[PDF 遮蔽完成] {input_path} -> {output_path}，共遮蔽 {len(entries)} 處")
    return entries


def restore_pdf(masked_path: str, output_path: str, map_file: str = DEFAULT_MAP_FILE):
    entries = _load_map_entry(map_file, os.path.basename(masked_path))
    if not entries:
        print("[找不到對照資料] 無法還原，請確認 --map 路徑與檔名是否正確")
        return
    doc = fitz.open(masked_path)
    for e in entries:
        page = doc[e["page"]]
        rect = fitz.Rect(*e["bbox"])
        page.insert_image(rect, stream=base64.b64decode(e["crop_b64"]))
    doc.save(output_path)
    doc.close()
    print(f"[PDF 還原完成] {masked_path} -> {output_path}，共還原 {len(entries)} 處")


# ========== CLI ==========
def main():
    parser = argparse.ArgumentParser(description="圖片 / PDF 去識別化(遮蔽) 與還原工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("mask-image", "restore-image", "mask-pdf", "restore-pdf"):
        p = sub.add_parser(name)
        p.add_argument("input", help="輸入檔案路徑")
        p.add_argument("output", help="輸出檔案路徑")
        p.add_argument("--map", default=DEFAULT_MAP_FILE, help="對照表檔案路徑 (預設 media_deid_map.json)")

    args = parser.parse_args()
    dispatch = {
        "mask-image": mask_image,
        "restore-image": restore_image,
        "mask-pdf": mask_pdf,
        "restore-pdf": restore_pdf,
    }
    dispatch[args.cmd](args.input, args.output, args.map)


if __name__ == "__main__":
    main()