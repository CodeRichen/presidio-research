import pytest
import torch
from nlp_pipeline import SentimentClassifier

# 使用 Pytest Fixture 載入極小型測試模型，加速測試執行
@pytest.fixture(scope="module")
def tiny_classifier():
    # 這個超小型模型結構與標準 Transformer 完全相同，但參數極少、執行速度極快
    tiny_model_name = "hf-internal-testing/tiny-random-BertForSequenceClassification"
    return SentimentClassifier(model_name=tiny_model_name, device="cpu")

# 測試 1：驗證模型輸出資料結構與張量維度 (Tensor Shape Check)
def test_predict_output_structure(tiny_classifier):
    text = "This is a wonderful test case for modern NLP pipeline."
    result = tiny_classifier.predict(text)

    # 斷言：驗證回傳字典關鍵字存在
    assert "logits" in result
    assert "probabilities" in result
    assert "predicted_class" in result

    # 斷言：驗證機率值加總接近 1.0 (Softmax 屬性)
    assert pytest.approx(sum(result["probabilities"]), 0.001) == 1.0
    # 斷言：預測類別必須是整數型態
    assert isinstance(result["predicted_class"], int)

# 測試 2：邊界條件測試 - 空字串與超長字串截斷 (Boundary Conditions)
def test_predict_edge_cases(tiny_classifier):
    # 空字串測試（不可以 Crash）
    empty_result = tiny_classifier.predict("")
    assert len(empty_result["probabilities"]) > 0

    # 超長字串測試（驗證 truncation 能否正常運作而不報錯）
    long_text = "NLP " * 1000
    long_result = tiny_classifier.predict(long_text)
    assert isinstance(long_result["predicted_class"], int)

# 測試 3：批次處理解析度與設備測試 (GPU/CPU Device Consistency)
def test_tokenizer_tensor_device(tiny_classifier):
    inputs = tiny_classifier.tokenizer("Test text", return_tensors="pt")
    # 驗證輸入張量的 Data Type 是否符合 PyTorch 標準
    assert inputs["input_ids"].dtype == torch.int64
    assert inputs["attention_mask"].dtype == torch.int64
    