"""agent_loop.py —— Agent 主循环

一个完整的 Agent Loop 就是这四步转圈：
    用户提问 → 模型决定「要不要调工具、调哪个」 → 我们执行工具 → 结果回传给模型
    → 模型继续决策 → …… → 直到它不再要工具，给出自然语言回答

关键点（面试必问）：
  · messages 是一个**不断变长**的列表，模型自己不记事，全靠每轮把历史重发一遍
  · 工具调用的结果必须以 role="tool" + tool_call_id 回填，否则模型对不上号
  · 必须有 MAX_STEPS 兜底，否则模型可能无限循环烧钱
  · 工具返回错误时**不抛异常**，而是把报错原文喂回去 —— 这就是 Self-Correction
"""

import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

from tools import TOOL_SCHEMAS, dispatch

# Windows 下把日志重定向到文件时不乱码（与 init_db.py 一致）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 最多允许多少轮「模型决策」，防止死循环把额度烧光
MAX_STEPS = 8

SYSTEM_PROMPT = """你是一个学校数据库（SQLite）的分析助手。用户用自然语言提问，你需要自己查库，最后用中文回答。

【工作规则】
1. 先探索，后查询。第一次接触这个库，必须先调用 list_tables；不知道某张表有哪些列，必须先调用 get_table_schema，绝不能猜列名。
2. 只写只读查询（SELECT / WITH），不要尝试任何修改数据的语句。
3. 一次只执行一条 SQL。复杂问题可以拆成多次查询，逐步缩小范围。
4. 如果 SQL 报错，仔细读报错信息（常见原因是表名/列名拼错，或 JOIN 少了关联条件），修正后重试，不要原样再发一次。
5. 查询结果为空时，先怀疑是自己的筛选条件写错了，而不是直接回答「没有数据」。
6. 涉及数值统计（平均分、最高分、排名）时：说清楚你的统计口径（例如「按每条成绩记录求平均」还是「先算每个学生的平均再按班求平均」），并给出排序依据的字段。
7. 一次查询能拿到的信息不要拆成多次，节省步骤。
8. 最终回答的格式：先给结论（数字 / 名单 / 排名），再给依据（用了哪几张表、做了什么筛选条件）。不要只给一段空话。

注意：你只能看到工具返回的数据，绝不允许编造数据库里没有的内容。"""


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
def _model() -> str:
    return os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()


def create_client() -> OpenAI:
    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key or api_key == "your_api_key_here":
        raise RuntimeError(
            "没配置 API Key。请把 .env.example 复制成 .env，并填入真实的 DEEPSEEK_API_KEY。"
        )
    return OpenAI(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
        timeout=60,
    )


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _brief(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"


def _thought_for(name: str, args: dict) -> str:
    """程序根据当前动作自动生成的「思考」，用于 ReAct 风格的可视化。"""
    if name == "list_tables":
        return "先看看这个库里有哪些表，别急着写 SQL。"
    if name == "get_table_schema":
        return f"不确定 {args.get('table_name')} 的结构，先查它的列、外键和样例数据。"
    if name == "execute_sql_query":
        return "表结构已经清楚，直接跑查询拿数据。"
    return f"调用工具 {name}。"


def _assistant_message(msg) -> dict:
    """把 SDK 返回的 message 转成可以塞回 messages 列表的 dict。

    自己拼 dict 而不是用 msg.model_dump()，是为了不依赖 openai 包的内部版本差异。
    """
    payload = {"role": "assistant", "content": msg.content or ""}
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        payload["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]
    return payload


# --------------------------------------------------------------------------
# 核心循环
# --------------------------------------------------------------------------
def run_agent(question: str, client: OpenAI | None = None,
              verbose: bool = True, max_steps: int = MAX_STEPS) -> dict:
    """跑一个完整的 Agent Loop，返回 {ok, answer, steps, trace}。"""
    client = client or create_client()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trace = []

    for step in range(1, max_steps + 1):
        response = client.chat.completions.create(
            model=_model(),
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0,
        )
        message = response.choices[0].message
        messages.append(_assistant_message(message))

        # —— 分支 A：模型不再要工具，说明它准备好回答了 ——
        if not message.tool_calls:
            if verbose:
                print(f"\n✅ 最终回答（第 {step} 轮决策）：{message.content}")
            return {"ok": True, "answer": message.content,
                    "steps": step, "trace": trace}

        # —— 分支 B：模型要调工具。可能一次调多个，必须逐个执行并逐个回填 ——
        for call in message.tool_calls:
            name = call.function.name
            raw_args = call.function.arguments or "{}"
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    args = {"_raw": args}
            except json.JSONDecodeError:
                args = {"_raw": raw_args}

            result = dispatch(name, raw_args)

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })

            thought = _thought_for(name, args)
            trace.append({
                "step": step,
                "thought": thought,
                "action": name,
                "action_input": args,
                "observation": result,
            })

            if verbose:
                print(f"\n── 第 {step} 步 ──")
                print(f"💭 思考：{thought}")
                print(f"🔧 动作：{name}({json.dumps(args, ensure_ascii=False)})")
                print(f"👀 观察：{_brief(result)}")

    # —— 兜底：步数用完还没给出答案，摘掉 tools 逼它收尾 ——
    if verbose:
        print(f"\n⚠️  已达最大步数 {max_steps}，要求模型基于已有信息收尾")
    messages.append({
        "role": "user",
        "content": (
            "已达到本次对话的最大工具调用步数。不要再调用任何工具，"
            "请直接基于上面已经查到的信息给出最终回答，并说明哪部分还没查清。"
        ),
    })
    response = client.chat.completions.create(
        model=_model(), messages=messages, temperature=0
    )
    answer = response.choices[0].message.content
    return {"ok": False, "answer": answer, "steps": max_steps,
            "trace": trace, "reason": "max_steps"}


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------
def main() -> None:
    load_dotenv()

    try:
        client = create_client()
    except RuntimeError as exc:
        sys.exit(f"[配置缺失] {exc}")

    # 带参数：一次性提问
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        result = run_agent(question, client=client)
        sys.exit(0 if result["ok"] else 0)

    # 不带参数：交互模式
    print("=" * 60)
    print("Text-to-SQL Agent 交互模式（输入 q 退出）")
    print("=" * 60)
    while True:
        try:
            question = input("\n❓ 你的问题：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if question.lower() in {"q", "quit", "exit"}:
            print("再见。")
            break
        if not question:
            continue
        try:
            run_agent(question, client=client)
        except Exception as exc:  # 单次失败不退出整个交互
            print(f"\n[出错] {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
