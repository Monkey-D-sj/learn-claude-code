---
description: 给 SQLite 加/改 CHECK 约束(改枚举取值)时的迁移做法:sessions.py 的 MIGRATIONS 结构里怎么重建表而不触发 CASCADE 删数据
---

# 改 SQLite 的 CHECK 约束(重建表)而不丢子表数据

适用:本项目的 `sessions.py`(以及任何"表上有 CHECK、现在要多一个取值"的场合,
比如给 `turns.status` 加一个新状态)。SQLite 改不了 CHECK,只能重建表 —— 而重建
里藏着一步会**静默删数据**。

## 关键坑(踩过一次,没报错)

`BEGIN IMMEDIATE` 里 `PRAGMA foreign_keys = OFF` 是 **no-op** —— SQLite 明确说
事务中改这个开关不生效。于是 `DROP TABLE turns` 会先做一次隐式 DELETE,而
`turn_messages.turn_id` 是 `ON DELETE CASCADE` —— **所有原始消息被删光,一句错
都不报**。

## 做法

1. 迁移步骤照常写在 `_MIGRATIONS` 的那串 SQL 里(建新表、`INSERT ... SELECT`、
   `DROP TABLE 旧表`、`ALTER TABLE 新表 RENAME TO 旧表`)。列序、UNIQUE、CHECK
   一个不少地照抄旧表定义。
2. 把这条迁移的版本号登记进 `NEEDS_FK_OFF`,让 `_migrate_no_fk` 用**另开的一个
   连接**(`foreign_keys` 默认就是 OFF)去跑它:
   `BEGIN IMMEDIATE` → 逐条执行 → `PRAGMA foreign_key_check` → `PRAGMA
   user_version = N` → `COMMIT`;任何一步抛就 `ROLLBACK`。
3. `foreign_key_check` 必须在 **COMMIT 之前**跑:提交之后再发现就只能人工修库。
4. RENAME 之后 SQLite 会自动把引用旧表名的外键指向新表(默认 `legacy_alter_table`
   关着),所以子表不用动。

## 验证(缺一不可)

- 用**纯 SQL** 造一个旧版本的库(照抄旧迁移的正文),不借新版代码的路径 ——
  改 `SCHEMA_VERSION` 再建一个 SessionStore 建出来的是"新版眼里的旧库",那正是
  被测的东西本身。
- 迁移后断言:子表行数不变(证明 DROP 没顺着 CASCADE 删)、`PRAGMA
  foreign_key_check` 为空、索引还在、`PRAGMA user_version` = 新版本、旧数据可读。
- 容错断言:把某个迁移步骤替换成坏 SQL → 抛异常后 `user_version` 还是旧值、
  新表/新列都不存在(整条回滚,不是迁一半)。
- 跑完之后**不要**在用户正在用的库上试:测试一律用 tmp_path 的临时库
  (`tests/conftest.py` 已经把 `SessionStore` 重定向了)。
