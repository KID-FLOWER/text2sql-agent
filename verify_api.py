"""verify_api.py —— 第三步：验证大模型 API 是否连通。

用法：
    1) copy .env.example .env
    2) 用记事本打开 .env，把 your_api_key_here 换成真实 Key
    3) python verify_api.py
"""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

# Windows 下把日志重定向到文件时不乱码（与 init_db.py 的做法一致）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

if not api_key or api_key == "your_api_key_here":
    sys.exit(
        "[配置缺失] 没找到真实 Key。\n"
        "  1. 在项目目录执行: copy .env.example .env\n"
        "  2. 打开 .env，把 your_api_key_here 换成你的 DeepSeek Key\n"
        "  3. 重新运行: python verify_api.py"
    )

client = OpenAI(api_key=api_key, base_url=base_url, timeout=30)

try:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "只回复四个字：API连接成功"}],
        temperature=0,
    )
except Exception as exc:  # 网络 / Key 错误 / 欠费都在这里显形
    sys.exit(f"[调用失败] {type(exc).__name__}: {exc}")

print("模型回复：", resp.choices[0].message.content)
print(f"用量：{resp.usage.prompt_tokens} 输入 / {resp.usage.completion_tokens} 输出 token")
print("✅ API 连通")