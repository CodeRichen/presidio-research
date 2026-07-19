#!/usr/bin/env python3
"""
PII Scanner - Git Pre-commit Hook
掃描 staged 檔案中的敏感資訊
"""

import sys
import subprocess
import spacy
from pathlib import Path

# 載入本地模型
MODEL_PATH = "./model-best"  # 改成你的模型路徑

# 要掃描的副檔名
SCAN_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".env", ".json", ".yaml", ".yml",
    ".sh", ".bash", ".txt", ".md",
    ".java", ".go", ".rb", ".php",
}

# 遮蔽字串
MASK = {
    "PERSON":       "[PERSON_REDACTED]",
    "API_KEY":      "[API_KEY_REDACTED]",
    "EMAIL_ADDRESS":"[EMAIL_REDACTED]",
    "PHONE_NUMBER": "[PHONE_REDACTED]",
    "IP_ADDRESS":   "[IP_REDACTED]",
    "CREDIT_CARD":  "[CREDIT_CARD_REDACTED]",
    "US_SSN":       "[SSN_REDACTED]",
    "IBAN_CODE":    "[IBAN_REDACTED]",
    "DATE_TIME":    "[DATE_REDACTED]",
    "LOCATION":     "[LOCATION_REDACTED]",
}

# 不想遮蔽的實體類型（可自訂）
IGNORE_LABELS = {"DATE_TIME", "LOCATION"}


def get_staged_files():
    """取得所有 staged 的檔案路徑"""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True
    )
    return result.stdout.strip().split("\n")


def scan_and_redact(text, nlp):
    """
    掃描文字並回傳：
    - found: 發現的實體列表
    - redacted_text: 遮蔽後的文字
    """
    doc = nlp(text)
    found = []
    redacted_text = text

    # 從後面往前取代，避免 offset 跑掉
    for ent in sorted(doc.ents, key=lambda e: e.start_char, reverse=True):
        if ent.label_ in IGNORE_LABELS:
            continue
        mask = MASK.get(ent.label_, f"[{ent.label_}_REDACTED]")
        found.append({
            "text": ent.text,
            "label": ent.label_,
            "start": ent.start_char,
            "end": ent.end_char,
        })
        redacted_text = (
            redacted_text[:ent.start_char] +
            mask +
            redacted_text[ent.end_char:]
        )

    return found, redacted_text


def main():
    print("🔍 PII Scanner 啟動中...")

    # 載入模型
    try:
        nlp = spacy.load(MODEL_PATH)
        print(f"✅ 模型載入成功：{MODEL_PATH}")
    except Exception as e:
        print(f"❌ 模型載入失敗：{e}")
        sys.exit(1)

    staged_files = get_staged_files()
    has_pii = False
    pii_report = []

    for filepath in staged_files:
        if not filepath:
            continue

        path = Path(filepath)

        # 只掃描指定副檔名
        if path.suffix not in SCAN_EXTENSIONS:
            continue

        # 跳過不存在的檔案
        if not path.exists():
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        found, redacted = scan_and_redact(content, nlp)

        if found:
            has_pii = True
            pii_report.append({
                "file": filepath,
                "entities": found,
                "redacted_content": redacted,
            })

    # ── 輸出報告 ────────────────────────────────────────────────
    if has_pii:
        print("\n" + "="*60)
        print("⚠️  發現敏感資訊！")
        print("="*60)

        for report in pii_report:
            print(f"\n📄 檔案：{report['file']}")
            for ent in report["entities"]:
                print(f"   [{ent['label']}] {ent['text']!r}  (位置 {ent['start']}~{ent['end']})")

        # ── 選擇：自動遮蔽 or 阻止 commit ───────────────────────
        print("\n選擇處理方式：")
        print("  [1] 自動遮蔽並繼續 commit")
        print("  [2] 阻止 commit（手動處理）")
        choice = input("請輸入 1 或 2：").strip()

        if choice == "1":
            for report in pii_report:
                Path(report["file"]).write_text(
                    report["redacted_content"], encoding="utf-8"
                )
                # 重新 stage 修改過的檔案
                subprocess.run(["git", "add", report["file"]])
            print("✅ 已自動遮蔽，繼續 commit")
            sys.exit(0)
        else:
            print("❌ Commit 已阻止，請手動處理敏感資訊後再試")
            sys.exit(1)
    else:
        print("✅ 未發現敏感資訊，繼續 commit")
        sys.exit(0)


if __name__ == "__main__":
    main()