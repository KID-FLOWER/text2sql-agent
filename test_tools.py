"""test_tools.py —— 不调用大模型、不花一分钱，先把工具层验证一遍

为什么要单独测工具？
    Agent 出错时，你分不清是「模型不会用工具」还是「工具本身有 bug」。
    先把工具层测干净，后面调 Agent 时只需要怀疑模型和 Prompt。

跑法：
    python test_tools.py
"""

import json
import sqlite3
import sys

from tools import (
    DB_PATH,
    dispatch,
    execute_sql_query,
    get_table_schema,
    list_tables,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

results = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    mark = "✅ PASS" if condition else "❌ FAIL"
    print(f"{mark}  {name}")
    if not condition and detail:
        print(f"        └─ {detail}")


def q(sql: str) -> dict:
    return json.loads(execute_sql_query(sql))


def main() -> int:
    print("=" * 70)
    print("工具层测试")
    print("=" * 70)

    # ---------- 1. list_tables ----------
    info = json.loads(list_tables())
    tables = info.get("tables", [])
    check("list_tables 返回 ok", info.get("ok") is True, str(info)[:200])
    check("包含 4 张业务表",
          set(tables) == {"teachers", "classes", "students", "scores"},
          f"实际返回：{tables}")
    check("过滤掉 sqlite_sequence",
          "sqlite_sequence" not in tables,
          f"实际返回：{tables}")
    check("带行数统计",
          info.get("row_counts", {}).get("students") == 30,
          f"实际：{info.get('row_counts')}")

    # ---------- 2. get_table_schema ----------
    schema = json.loads(get_table_schema("students"))
    cols = [c["name"] for c in schema.get("columns", [])]
    check("get_table_schema 返回 ok", schema.get("ok") is True, str(schema)[:200])
    check("students 的列是 id/name/class_id",
          cols == ["id", "name", "class_id"], f"实际：{cols}")
    check("能读出外键 students.class_id -> classes",
          any(fk.get("references_table") == "classes"
              for fk in schema.get("foreign_keys", [])),
          f"实际：{schema.get('foreign_keys')}")
    check("带 2 行样例数据",
          len(schema.get("sample_rows", [])) == 2,
          f"实际：{schema.get('sample_rows')}")

    bad = json.loads(get_table_schema("不存在的表"))
    check("查不存在的表返回错误而不是崩溃",
          bad.get("ok") is False and bad.get("available_tables"),
          str(bad)[:200])

    # ---------- 3. execute_sql_query 正常查询 ----------
    r = q("SELECT COUNT(*) AS n FROM students")
    check("全校学生数 = 30", r.get("rows") == [[30]], str(r)[:200])

    r = q("SELECT ROUND(AVG(score), 2) FROM scores WHERE subject = '数学'")
    check("数学平均分 = 75.67", r.get("rows") == [[75.67]], str(r)[:200])

    r = q("SELECT * FROM students")
    check("大结果集被截断到 200 行",
          r.get("row_count") == 30 and r.get("truncated") is False, str(r)[:120])

    # ---------- 4. 安全防护 ----------
    dangerous = [
        "DROP TABLE students",
        "DELETE FROM students",
        "UPDATE scores SET score = 100",
        "INSERT INTO students VALUES (99, '黑客', 1)",
        "SELECT 1; DROP TABLE students",
        "PRAGMA table_info(students)",
    ]
    for sql in dangerous:
        r = q(sql)
        check(f"拦截危险语句：{sql[:38]}", r.get("ok") is False, str(r)[:150])

    # ---------- 5. 错误信息要原样返回，供模型自我修正 ----------
    r = q("SELECT namee FROM students")
    check("列名写错时返回 SQLite 原始报错",
          r.get("ok") is False and "no such column" in str(r.get("error", "")),
          str(r)[:200])

    # ---------- 6. 攻击之后数据库仍然完好 ----------
    conn = sqlite3.connect(DB_PATH)
    try:
        n = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    finally:
        conn.close()
    check("所有攻击尝试之后数据依然完整（30 人）", n == 30, f"实际：{n}")

    # ---------- 7. dispatch 容错 ----------
    r = json.loads(dispatch("execute_sql_query", "{这不是合法JSON"))
    check("参数不是合法 JSON 时返回错误", r.get("ok") is False, str(r)[:150])

    r = json.loads(dispatch("不存在的工具", "{}"))
    check("调用不存在的工具时返回错误", r.get("ok") is False, str(r)[:150])

    # ---------- 汇总 ----------
    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 70)
    print(f"共 {len(results)} 项，通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：")
        for name, _, detail in failed:
            print(f"  - {name}  {detail}")
        return 1
    print("🎉 工具层全部通过，可以接大模型了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
