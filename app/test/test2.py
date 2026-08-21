from transformers import AutoTokenizer

string = "Only those who will risk going too far can possibly find out how far one can go."

model_name = "distilbert-base-uncased-finetuned-sst-2-english" #直接叫model名字
tokenizer = AutoTokenizer.from_pretrained(model_name)
# 直接呼叫 tokenizer 處理字串
inputs = tokenizer(string, return_tensors="pt") # return_tensors="pt" 代表傳回 PyTorch Tensor

print(inputs)
# 觀察拆解後的 Token（詞元）
tokens = tokenizer.convert_ids_to_tokens(inputs["input_ids"][0])
print(tokens)
# 輸出：['[CLS]', 'only', 'those', 'who', 'will', 'risk', 'going', 'too', 'far', 'can', 'possibly', 'find', 'out', 'how', 'far', 'one', 'can', 'go', '.', '[SEP]']
