"""tools.py —— 工具层：给模型一张「这个数据库能干什么」的说明书

三个工具（必须与 agent_loop.py 里的 JSON Schema 一一对应）：
  1. list_tables()              这个库有哪些业务表、各有多少行
  2. get_table_schema(table)    某张表有哪些列、什么类型、外键指向谁、样例长什么样
  3. execute_sql_query(sql)     执行一条**只读** SQL，返回结构化结果

安全设计（三道锁，缺一不可）：
  ① 语句校验：只放行 SELECT / WITH 开头的**单条**语句
  ② 只读连接：sqlite 以 `mode=ro` 打开，物理上写不进去
  ③ query_only：连接级 PRAGMA 再兜一层

设计原则：工具出错时**返回错误信息而不是抛异常**，
因为错误信息是喂回给模型自我修正的原料（Self-Correction）。
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "school.db"

# 单次查询最多回传多少行，防止一条 SELECT * 把上下文撑爆
MAX_ROWS = 200

# SQLite 内部表（sqlite_sequence 是 AUTOINCREMENT 自动生成的，必须过滤掉）
SYSTEM_TABLE_PREFIX = "sqlite_"


class ToolError(Exception):
    """工具层的业务异常。"""


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise ToolError(f"数据库不存在：{DB_PATH}（请先运行 python init_db.py）")
    # mode=ro 是关键：即使前面的语句校验被绕过，写入也会被 SQLite 拒绝
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _business_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    # 过滤 sqlite_master / sqlite_sequence 这类内部表，只留业务表
    return [r["name"] for r in rows if not r["name"].startswith(SYSTEM_TABLE_PREFIX)]


# --------------------------------------------------------------------------
# 工具 1：列出所有表
# --------------------------------------------------------------------------
def list_tables() -> str:
    """列出数据库里所有业务表，并附带行数。"""
    try:
        conn = _connect()
    except (ToolError, sqlite3.Error) as exc:
        return _json({"ok": False, "error": str(exc)})

    try:
        names = _business_tables(conn)
        counts = {}
        for name in names:
            counts[name] = conn.execute(
                f'SELECT COUNT(*) AS n FROM "{name}"'
            ).fetchone()["n"]
        return _json({"ok": True, "tables": names, "row_counts": counts})
    except sqlite3.Error as exc:
        return _json({"ok": False, "error": f"读取表清单失败：{exc}"})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 工具 2：查看某张表的结构
# --------------------------------------------------------------------------
def get_table_schema(table_name: str) -> str:
    """返回一张表的列定义、外键关系和最多 2 行样例数据。"""
    if not table_name or not isinstance(table_name, str):
        return _json({"ok": False, "error": "table_name 必须是非空字符串"})

    try:
        conn = _connect()
    except (ToolError, sqlite3.Error) as exc:
        return _json({"ok": False, "error": str(exc)})

    try:
        names = _business_tables(conn)
        if table_name not in names:
            return _json({
                "ok": False,
                "error": f"表 {table_name!r} 不存在",
                "available_tables": names,
            })

        # PRAGMA table_info：cid, name, type, notnull, dflt_value, pk
        info = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        columns = [
            {
                "name": c["name"],
                "type": c["type"],
                "not_null": bool(c["notnull"]),
                "primary_key": bool(c["pk"]),
            }
            for c in info
        ]

        # PRAGMA foreign_key_list：从这张表指出去的引用
        fks = conn.execute(f'PRAGMA foreign_key_list("{table_name}")').fetchall()
        foreign_keys = [
            {"column": f["from"], "references_table": f["table"],
             "references_column": f["to"]}
            for f in fks
        ]

        # 样例数据：让模型知道 subject 里存的是「数学」还是「math」
        sample_rows = conn.execute(
            f'SELECT * FROM "{table_name}" LIMIT 2'
        ).fetchall()
        sample = [dict(r) for r in sample_rows]
        total = conn.execute(
            f'SELECT COUNT(*) AS n FROM "{table_name}"'
        ).fetchone()["n"]

        return _json({
            "ok": True,
            "table": table_name,
            "columns": columns,
            "foreign_keys": foreign_keys,
            "row_count": total,
            "sample_rows": sample,
        })
    except sqlite3.Error as exc:
        return _json({"ok": False, "error": f"读取 {table_name} 结构失败：{exc}"})
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 工具 3：执行只读 SQL
# --------------------------------------------------------------------------
def _check_sql(sql: str) -> str:
    """校验语句合法性，返回规范化后的 SQL；不合法就抛 ToolError。"""
    if not sql or not sql.strip():
        raise ToolError("SQL 为空")

    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise ToolError("SQL 为空")

    # 只允许单条语句：去掉末尾分号后，中间不允许再有分号
    # （字符串字面量里带分号的情况极少，这里按工程上的保守策略处理）
    if ";" in statement:
        raise ToolError("只允许一次执行一条 SQL 语句，不要用分号拼接多条")

    first_word = statement.split(None, 1)[0].lower()
    if first_word not in ("select", "with"):
        raise ToolError(
            f"只允许 SELECT 查询，检测到 {first_word.upper()}。"
            "本工具是只读的，不能修改数据或结构。"
        )
    return statement


def execute_sql_query(sql: str) -> str:
    """执行 SELECT 查询，返回 {columns, rows, row_count}。"""
    try:
        statement = _check_sql(sql)
    except ToolError as exc:
        return _json({"ok": False, "error": str(exc)})

    try:
        conn = _connect()
    except (ToolError, sqlite3.Error) as exc:
        return _json({"ok": False, "error": str(exc)})

    try:
        cursor = conn.execute(statement)
        rows = cursor.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        columns = [d[0] for d in cursor.description] if cursor.description else []
        data = [[row[c] for c in columns] for row in rows]
        return _json({
            "ok": True,
            "columns": columns,
            "rows": data,
            "row_count": len(data),
            "truncated": truncated,
        })
    except sqlite3.Error as exc:
        # ★ 关键：把 SQLite 的原话原封不动还给模型，它才知道怎么改
        return _json({
            "ok": False,
            "error": f"SQLite 报错：{exc}",
            "your_sql": statement,
            "hint": "请检查表名、列名、JOIN 条件是否拼写正确，然后修正 SQL 重试",
        })
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 供 agent_loop 使用：OpenAI Tool Calling 规范的三份 JSON Schema
# --------------------------------------------------------------------------
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": (
                "列出数据库里所有业务表的名字和行数。"
                "★ 这是探索数据库的第一步，任何不确定表名的时候都应该先调用它。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_table_schema",
            "description": (
                "查看某张表的完整结构：列名、类型、主键、外键指向哪张表，以及 2 行样例数据。"
                "★ 写 SQL 之前不知道该用哪个列名、或不知道某列存的是什么值（比如科目叫「数学」还是「math」）"
                "时，必须先调用它，不要猜。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "表名，例如 students、scores",
                    }
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sql_query",
            "description": (
                "执行一条 SQLite 只读查询（只允许 SELECT / WITH，禁止增删改）。"
                "返回列名、数据行、行数；如果 SQL 有语法或逻辑错误，会返回 SQLite 的原始报错信息，"
                "请读懂报错后修正 SQL 再调用一次。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "一条完整的 SELECT 语句，不要带分号，不要写多条语句",
                    }
                },
                "required": ["sql"],
            },
        },
    },
]

# 名称 → 真正的执行函数（agent_loop 的调度表）
TOOL_FUNCTIONS = {
    "list_tables": list_tables,
    "get_table_schema": get_table_schema,
    "execute_sql_query": execute_sql_query,
}


def dispatch(name: str, arguments_json: str) -> str:
    """模型给出「函数名 + JSON 字符串参数」，这里负责解析并执行。

    永远返回字符串（即使出错），因为消息里的 content 必须是字符串。
    """
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return _json({"ok": False, "error": f"未知工具：{name}"})

    try:
        args = json.loads(arguments_json) if arguments_json else {}
        if not isinstance(args, dict):
            raise ValueError("参数必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as exc:
        return _json({"ok": False, "error": f"参数不是合法 JSON：{exc}"})

    try:
        return fn(**args)
    except TypeError as exc:
        return _json({"ok": False, "error": f"参数与工具签名不匹配：{exc}"})
    except Exception as exc:  # 兜底，绝不让工具异常打断主循环
        return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
