# GLM4.6V Call Template

Use placeholders only for secrets. Never write or log real keys in artifacts.

## Environment Variables

```bash
export ZAI_API_KEY="<YOUR_KEY>"
export GLM_BASE_URL="https://api.z.ai/api/paas/v4"
export GLM_MODEL_ID="glm-4.6v"
```

## curl (image_url with public URL)

```bash
curl --request POST \
  --url "${GLM_BASE_URL}/chat/completions" \
  --header "Authorization: Bearer ${ZAI_API_KEY}" \
  --header "Content-Type: application/json" \
  --data '{
    "model": "glm-4.6v",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "请描述这张图"},
          {"type": "image_url", "image_url": {"url": "https://example.com/demo.jpg"}}
        ]
      }
    ]
  }'
```

## Python (OpenAI-compatible SDK)

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("ZAI_API_KEY"),
    base_url=os.getenv("GLM_BASE_URL", "https://api.z.ai/api/paas/v4"),
)

resp = client.chat.completions.create(
    model=os.getenv("GLM_MODEL_ID", "glm-4.6v"),
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "图中有什么？"},
                {"type": "image_url", "image_url": {"url": "https://example.com/demo.jpg"}},
            ],
        }
    ],
)
print(resp.choices[0].message.content)
```

## Local Image as base64 Data URL

```python
import base64

with open("demo.jpg", "rb") as f:
    b64 = base64.b64encode(f.read()).decode("utf-8")
data_url = f"data:image/jpeg;base64,{b64}"
```

Then set:

```json
{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,<...>"}}
```
