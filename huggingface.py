import torch
from transformers import AutoModelForImageTextToText, AutoProcessor

model_id = "orcarouter/Qwen3.8-27B-Uncensored-FP8"

# 1. Load Processor & Model
processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
model = AutoModelForImageTextToText.from_pretrained(
    model_id,
    device_map="auto",
    torch_dtype="auto",
    offload_folder="./offload",
    trust_remote_code=True
)

# 2. Format chat messages (supports text and/or images)
messages = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "Write a short summary of how abliteration works in LLMs."}
        ],
    }
]

# 3. Apply chat template and tokenize
prompt = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
inputs = processor(text=[prompt], return_tensors="pt").to("cuda")

# 4. Generate
generated_ids = model.generate(**inputs, max_new_tokens=32)
generated_ids_trimmed = [
    out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
]

output_text = processor.batch_decode(
    generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
)
print(output_text[0], flush=True)