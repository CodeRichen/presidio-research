import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

class SentimentClassifier:
    def __init__(self, model_name: str = "distilbert-base-uncased", device: str = "cpu"):
        self.device = device
        # 載入分詞器與分類模型
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name).to(self.device)
        self.model.eval()

    def predict(self, text: str) -> dict:
        """對單一文字進行預測，回傳類別機率與最高分的類別 ID"""
        inputs = self.tokenizer(
            text, 
            return_tensors="pt", 
            truncation=True, 
            max_length=128
        ).to(self.device)

        with torch.no_grad():
            outputs = self.model(**inputs)
            logits = outputs.logits
            # 使用 Softmax 將 Logits 轉為機率分佈
            probabilities = torch.softmax(logits, dim=-1).squeeze(0)
            predicted_class = torch.argmax(probabilities).item()

        return {
            "logits": logits,
            "probabilities": probabilities.tolist(),
            "predicted_class": predicted_class
        }