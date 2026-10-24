# Text-to-SQL Agent（路线 A）

自然语言提问 → Agent 自主探索 SQLite 学校库 → 生成并执行 SQL → 用中文回答。

## 表关系

```
teachers 1─N classes 1─N students 1─N scores
```

数据库由老师提供的 `init_db.py` 生成（**该文件原样保留，未做任何修改**）：6 位教师 / 5 个班 / 30 名学生 / 148 条成绩。

## 文件

| 文件 | 说明 |
|---|---|
| `init_db.py` | 老师提供，原样未改 |
| `tools.py` | 三个工具（列表 / 表结构 / 执行 SQL）+ OpenAI Tool Calling 的 JSON Schema + 安全防护 |
| `agent_loop.py` | Agent 主循环、System Prompt、MAX_STEPS 兜底 |
| `test_tools.py` | 工具层单元测试，22 项，**不花钱** |
| `test_queries.py` | 题目指定的 5 个问题，跑完自动存档到 `logs/` |
| `verify_api.py` | 最小连通性验证 |
| `docs/项目讲解.md` | 面试用的原理速查 |

## 跑起来

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          :: 然后填入真实 Key
python init_db.py
python verify_api.py            :: 第 1 关：确认 Key 和网络通
python test_tools.py            :: 第 2 关：确认工具层没 bug（不花额度）
python test_queries.py          :: 第 3 关：跑完整 5 题
```

单题调试：

```bat
python test_queries.py 3        :: 只跑第 3 题（多表 JOIN）
python agent_loop.py "全校有多少学生？"   :: 直接问一句
```

## 设计要点

- **先探索后查询**：模型必须先 `list_tables`，不确定字段必须 `get_table_schema`，不允许猜列名。
- **三道安全锁**：语句校验（只放行单条 SELECT/WITH）+ `mode=ro` 只读连接 + `PRAGMA query_only`。
- **Self-Correction**：工具不抛异常，而是把 SQLite 的原始报错字符串回传给模型，让它自己改 SQL 重试。
- **防线兜底**：`MAX_STEPS = 8`，用完则摘掉 `tools` 参数强制模型基于已有信息作答。

## 安全

- `.env` 已加入 `.gitignore`，仓库里只有 `.env.example`（占位符）。
- `school.db` 不入库，随时可由 `init_db.py` 重建。
> 这是我的第一次 Pull Request 练习。
