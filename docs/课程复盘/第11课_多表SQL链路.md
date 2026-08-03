# 第11课：多表 SQL 生成、校验与执行

> 日期：2026-08-03
> 状态：已完成

---

## 一、问题

第10课实现了多表 Schema 加载，但 SQL 生成和校验链路仍按单表设计——降级 SQL 只写 `FROM table` 不支持 JOIN，校验只检查单表名，LLM 审计 prompt 不知道有 JOIN 约束。

## 二、根因

- `_build_fallback_sql()` 硬编码单表 `FROM {table}`，未读取 `confirmed_plan.joins`
- `_validate_sql_against_plan()` 只校验 `table` 字段，未遍历 `tables` 列表
- `_check_plan_consistency()` 的 prompt 内联在代码中且不包含 JOIN 信息
- `confirmed_section` 构造时未拼接多表字段来源和 JOIN 约束
- `validate_sql_node` 无笛卡尔积检测、JOIN 键匹配等安全校验

## 三、实现内容

### 新建文件

- `prompts/sql_audit_prompt.py`：将 LLM 审计 prompt 从代码中抽取为独立的 `SQL_AUDIT_SYSTEM_PROMPT` + `SQL_AUDIT_HUMAN_TEMPLATE`

### 修改文件

- `generate_sql_node.py`：
  - `_build_fallback_sql()` 增加 `from_clause` + JOIN 子句拼接
  - `_validate_sql_against_plan()` 遍历 `tables` 校验所有表
  - `_check_plan_consistency()` 引用外部 prompt 并传入 `joins` 信息
  - `confirmed_section` 加入表列表、字段来源、JOIN 约束描述
- `validate_sql_node.py`：
  - 新增 `_validate_multi_table()`：笛卡尔积检测（禁止逗号分隔多表）、表完整性、JOIN 键匹配

### 多表 SQL 全链路

```
confirmed_plan → CoverageAnalyzer → JoinPlanner
  → Schema Resolver（多表加载 + JOIN 格式化）
  → Generate SQL（prompt 含 JOIN 约束）
  → Validate SQL（语法 + 资源保护 + 多表安全三道防线）
  → Execute SQL
```

## 四、面试表达

> "多表场景下，SQL 生成 prompt 中明确包含 JOIN 约束（关联表、关联键、JOIN 类型），LLM 只能按方案执行，不允许自创关联。校验环节增加了笛卡尔积检测和 JOIN 键匹配，用正则做轻量级 AST 分析，不引入额外依赖。"
