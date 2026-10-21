"""test_queries.py —— 用题目指定的 5 个问题测试 Agent

每道题都会：
  1. 跑一次完整 Agent Loop（打印每一步的 思考/动作/观察）
  2. 记录答案、用了多少步、耗时、调用了哪些工具
  3. 把完整轨迹存到 logs/ 下，写实验记录时直接抄

跑法：
    python test_queries.py            # 全部 5 题
    python test_queries.py 3          # 只跑第 3 题（多表 JOIN）
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from agent_loop import create_client, run_agent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LOGS_DIR = Path(__file__).resolve().parent / "logs"

QUERIES = [
    ("① 简单查询",       "全校有多少学生？"),
    ("② 单表聚合",       "全校数学平均分是多少？"),
    ("③ 多表 JOIN（核心）", "张老师带的班里，编程成绩前三名是谁？"),
    ("④ 多表聚合 + 排序",  "哪个班平均成绩最高？"),
    ("⑤ 数据探索",       "有没有缺考的学生？"),
]


def summarize_trace(trace: list[dict]) -> str:
    """把轨迹压成一行，方便写总结表格。"""
    if not trace:
        return "（没有调用任何工具，直接作答 —— 需要检查 Prompt）"
    return " → ".join(f"{t['action']}" for t in trace)


def main() -> int:
    only = None
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        only = int(sys.argv[1]) - 1

    try:
        client = create_client()
    except RuntimeError as exc:
        sys.exit(f"[配置缺失] {exc}\n先填好 .env 再来跑测试。")

    LOGS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    records = []

    print("=" * 78)
    print(f"Text-to-SQL Agent 测试 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 78)

    for index, (label, question) in enumerate(QUERIES):
        if only is not None and index != only:
            continue

        print(f"\n{'━' * 78}")
        print(f"{label}  {question}")
        print("━" * 78)

        started = time.time()
        try:
            result = run_agent(question, client=client)
            answer, ok, steps = result["answer"], result["ok"], result["steps"]
            trace = result["trace"]
        except Exception as exc:  # 单题失败不影响后面的题
            answer, ok, steps, trace = f"[异常] {type(exc).__name__}: {exc}", False, 0, []
            print(f"\n[异常] {type(exc).__name__}: {exc}")

        elapsed = time.time() - started
        records.append({
            "label": label,
            "question": question,
            "answer": answer,
            "ok": ok,
            "steps": steps,
            "elapsed_sec": round(elapsed, 1),
            "tool_chain": summarize_trace(trace),
            "trace": trace,
        })
        print(f"\n⏱  {elapsed:.1f} 秒 · {steps} 轮决策 · 工具链：{summarize_trace(trace)}")

    # ---------- 存档 ----------
    if records:
        (LOGS_DIR / f"run_{stamp}.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [f"# Agent 测试记录 {stamp}", ""]
        for r in records:
            lines += [
                f"## {r['label']}",
                f"**问题**：{r['question']}",
                "",
                f"**回答**：{r['answer']}",
                "",
                f"- 轮次：{r['steps']}　耗时：{r['elapsed_sec']}s　状态：{'成功' if r['ok'] else '异常'}",
                f"- 工具链：{r['tool_chain']}",
                "",
            ]
            for t in r["trace"]:
                lines.append(f"  - 💭 {t['thought']}")
                lines.append(f"    🔧 `{t['action']}({json.dumps(t['action_input'], ensure_ascii=False)})`")
                lines.append(f"    👀 `{t['observation'][:300]}`")
            lines.append("")
        (LOGS_DIR / f"run_{stamp}.md").write_text("\n".join(lines), encoding="utf-8")

        print("\n" + "=" * 78)
        print("测试汇总")
        print("=" * 78)
        print(f"{'题目':<14}{'轮次':<6}{'耗时':<9}{'状态':<8}工具链")
        for r in records:
            print(f"{r['label']:<14}{r['steps']:<6}{r['elapsed_sec']:<9}"
                  f"{'成功' if r['ok'] else '异常':<8}{r['tool_chain']}")
        print(f"\n完整轨迹已存到：logs/run_{stamp}.md")

    return 0 if all(r["ok"] for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
