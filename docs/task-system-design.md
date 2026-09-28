# Task 与 Agent 工具设计

文档状态：**已实施，后续将 `agent` 与 task 解耦（2026-09-28）**。适用范围是当前单进程、本机使用的 Agent 服务。本文中的 task 是跨会话的工作项；turn 是一次聊天请求的执行记录，两者不是同一个对象。

## 1. 目标与边界

- 用持久化的 task 和依赖图替换现有的 `todo_write` 清单。
- task 属于当前服务使用的数据库，不绑定创建它的会话。新会话开启 task 功能后，可以读取并继续已有任务。
- task 功能按会话开启或关闭，新会话默认关闭。关闭只隐藏 task 能力，不删除任务。
- 保留独立委派能力：`agent` 只接收 `prompt`，不认领或修改 task。任务由 `task_*` 工具单独管理。
- 第一版不做自动调度、并行子 agent、任务硬删除、执行历史表或多用户权限控制。

## 2. 数据模型

在现有 `sessions.db` 中增加两张全局表，并给 `sessions` 增加一个开关字段。task 表不含 `session_id`。

| `tasks` 字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `id` | `TEXT PRIMARY KEY` | 服务端生成的 UUID；创建后不变 |
| `name` | `TEXT NOT NULL`，去空白后非空 | 简短标题 |
| `description` | `TEXT NOT NULL DEFAULT ''` | 具体要求及完成标准 |
| `owner` | `TEXT NOT NULL DEFAULT 'main'`，去空白后非空 | 稳定的负责人标签，不是某次子 agent 调用的 ID |
| `status` | `TEXT NOT NULL DEFAULT 'pending'`，见下方状态集合 | 当前任务状态 |
| `created_at` | `REAL NOT NULL` | 创建时间 |
| `updated_at` | `REAL NOT NULL` | 最后修改时间 |

`status` 只存 `pending`、`in_progress`、`completed`、`cancelled`。`blocked` 和 `ready` 根据依赖实时计算，不落库。列表默认按 `created_at, id` 排序；第一版不支持手动排序，因此不需要 `position`。

| `task_dependencies` 字段 | 约束 | 含义 |
| --- | --- | --- |
| `task_id` | 外键指向 `tasks.id` | 后续任务 |
| `depends_on_id` | 外键指向 `tasks.id` | 必须先完成的任务 |

两列组成联合主键，并约束 `task_id <> depends_on_id`。对 `depends_on_id` 建索引，方便查找受某任务影响的后续任务。第一版不提供硬删除；取消任务保留其依赖边和历史。

`sessions.task_enabled` 为布尔值，数据库中用 `INTEGER NOT NULL DEFAULT 0 CHECK (task_enabled IN (0, 1))` 保存。它只决定该会话能否使用 task 工具，不决定任务归属。`owner` 目前是工作分配标签，不承担权限校验；默认值为 `main`。

不增加 `revision`、`active_run_id`、`task_runs` 或任务来源会话字段。不同会话同时编辑同一条待执行任务时，同一字段采用最后一次成功写入的值；工具只更新明确传入的字段，不能用整行覆盖。未来若实际出现编辑冲突，再引入版本校验。

## 3. 依赖与状态规则

任务图是有向无环图。新增依赖时，在同一短事务内检查两个 ID 存在、不是同一个任务、边尚不存在，并通过图遍历或递归查询拒绝成环。修改依赖只允许目标任务处于 `pending`。

- `ready`：任务为 `pending`，且所有直接依赖均为 `completed`；没有依赖的任务天然 ready。
- `blocked`：任务为 `pending`，且至少一个直接依赖未完成。依赖被取消时，后续任务继续 blocked，直到用户调整依赖或另建替代任务。
- `pending → in_progress`：必须 ready；用条件更新原子认领，影响行数为 1 才能开始执行。
- `pending → cancelled`：不再执行；已完成和已取消任务为终态。
- `in_progress → completed`：执行结果经主 agent 判断达到要求后，由 `task_status` 显式提交。
- `in_progress → pending`：用于重试，仅在原执行已停止后允许；不因超时自动重试。

运行中的任务不能修改 `name`、`description`、`owner` 或依赖，也不能再次认领。已完成任务不会因为其前置任务后来发生变化而倒退；第一版禁止修改或取消已完成任务，保持依赖判断稳定。

认领与依赖检查必须在同一事务中完成，不能先读状态、释放锁，再无条件更新。示意：

```sql
UPDATE tasks
SET status = 'in_progress', updated_at = ?
WHERE id = ? AND status = 'pending'
  AND NOT EXISTS (
    SELECT 1
    FROM task_dependencies AS d
    JOIN tasks AS prerequisite ON prerequisite.id = d.depends_on_id
    WHERE d.task_id = tasks.id AND prerequisite.status <> 'completed'
  );
```

影响行数为 0 时返回当前状态及未完成的依赖，不启动 agent，也不产生模型调用成本。所有任务写入使用现有 `SessionStore` 的短事务；事务不得包住模型请求或子 agent 执行。SQLite 的写入仍会串行，但任务创建和状态变化频率远低于模型输出事件；第一版不另开数据库。

## 4. 工具接口

共六个工具。前五个属于 task 功能；`agent` 始终可用。读写分开，因为当前 `ToolDesc.side_effect` 是按整个工具标记的，读操作必须是 `False`，写操作必须是 `True`。

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `task_read` | 可选 `task_id`、`owner`、`status` | 有 `task_id` 时返回详情和依赖；否则列任务，包含 ready/blocked 及阻塞原因 |
| `task_create` | `name`；可选 `description`、`owner`、`depends_on_ids` | 生成 ID；任务和初始依赖在同一事务提交，默认 owner 为 `main` |
| `task_edit` | `task_id`；`name`、`description`、`owner` 至少一个 | 只改传入字段；任务必须处于 `pending` |
| `task_dependency` | `task_id`、`depends_on_id`、`action: add/remove` | 在事务中校验并修改依赖；目标任务必须处于 `pending` |
| `task_status` | `task_id`、`action: start/complete/cancel/retry` | 按状态规则迁移；`start` 原子认领 ready 任务 |
| `agent` | 必填 `prompt` | 独立委派子 agent，不读写 task 表 |

`task_read` 列表默认返回未完成任务，并允许显式查询已完成或已取消任务。`status` 过滤可使用数据库状态以及计算出的 `ready`、`blocked`。所有工具结果返回机器可读的任务 ID、实际状态和错误原因，不能只打印人类可读的清单。对不存在的任务、非法状态迁移和依赖成环，返回明确错误，不静默修正。

`agent` 只执行传入的自包含 `prompt`。若主 agent 同时在处理一条 task，应先用 `task_status(start)` 认领，再根据任务内容独立组织工作；子 agent 的返回不会修改任务状态。主 agent 检查结果后显式调用 `task_status(complete)`，未达到要求且原执行已结束时可显式 `retry`。执行失败或进程中断时保留可检查的 `in_progress` 状态，不自动重跑。

`task_status(start)` 是唯一的任务认领入口。子 agent 不获得 task 工具，也不能再调用 `agent`；它的报告只交回主 agent，由主 agent 决定是否更新任务表。

## 5. 运行中任务与重试

同一数据库仍由一个服务进程使用。服务端在内存中记录 `task_status(start)` 认领的 task ID 及其所属 turn，占用保持到任务提交完成或当前 turn 结束。其他会话不能在执行期间替它完成或重试。该记录不是持久化的任务身份，也不放进 task 表。

`retry` 是显式操作，服务端发现原执行仍在进行时必须拒绝。进程意外退出后，原进程的执行已经停止，但 task 仍可能留下部分文件修改或其他副作用；恢复时展示为待核对，不自动把它改回 `pending`。核对后再显式重试。不能因为一个定时器到期就自动重跑。

`owner` 不解决“哪一次执行正在运行”的问题；第一版也不试图支持同一任务在原执行未结束时抢占、转交或重新运行。若以后要支持这些动作，再引入持久化的执行 ID 与相应的完成校验。

## 6. 会话开关与页面

- 新会话的 `task_enabled` 默认为关闭，页面提供开关并展示当前值。
- 关闭时不向模型提供任何 `task_*` 工具，也不注入任务清单或任务提醒；`agent(prompt)` 仍可使用。
- 开启时提供五个 task 工具；`agent` 的接口不随开关改变。任务由数据库读取，跨会话可见。
- 开关更新由服务端保存到 `sessions.task_enabled`。活动 turn 或尚未解决的中断 turn 存在时拒绝切换；完成或处理后再切换，从下一轮生效。这样一轮执行期间的工具集和恢复签名保持不变。
- 关闭不会删除或取消任务；再次开启后继续读取同一张全局表。

任务列表可在页面中展示，但页面状态以数据库查询为准，不从聊天事件重建。任务变更仍可发页面事件用于即时刷新；事件只是展示记录，不是任务数据的权威来源。

## 7. 从 Todo 迁移

移除 `todo_write`、`TodoManager`、每会话的 `TODOS` 容器、每三轮一次的 todo 提醒，以及 checkpoint `runtime_json` 中新增和恢复 `todos` 的逻辑。现有 `task(prompt)` 工具改名为 `agent`，同步更新工具注册、系统提示及相关测试。

旧会话中的 `todo_write` 调用结果和旧 checkpoint 字段作为历史资料保留，不自动转成新 task。旧 todo 条目没有稳定 ID、owner 或依赖信息，自动导入可能造成重复。新版本的工具集会改变恢复签名；部署切换前应处理未解决的中断 turn，切换后不能绕过签名校验强行恢复旧工具调用。数据库迁移只增加新表和开关列，不删除历史消息或事件。

## 8. 验收要点

1. 两个会话开启 task 后能看到同一任务；任一会话关闭 task 不影响另一会话或任务数据。
2. 两个会话同时用 `task_status(start)` 认领同一 ready 任务，恰好一个成功；`agent` 调用不参与认领。
3. 多前置任务全部完成后任务才 ready；自依赖、重复边和环均被拒绝。
4. 任务运行中不能修改要求或依赖；`agent(prompt)` 返回后不会自动改变任务状态。
5. 运行中、失败、中断及服务重启后均不自动重试；显式 retry 只能在原执行停止后进行。
6. task 关闭时模型看不到 task 工具和任务内容，`agent(prompt)` 仍可用；旧会话历史可读。
7. 任务表写入和依赖边写入保持原子性；模型请求及 `agent(prompt)` 执行期间不持有 SQLite 事务。

## 9. 实施记录

七条验收要点由 `tests/test_task.py` 覆盖。落地时有两处正文没定死、
由实施时决定的地方，记在这儿：

- **模型怎么看见任务：只靠 `task_read`，不做任何自动注入。** 第 6 节那句
  "关闭时……也不注入任务清单或任务提醒"只删掉了旧的 todo 注入；开启时同样
  不注入，模型要清单就自己调 `task_read`。好处是 system prompt 逐字节不随
  任务变化，前缀缓存不被任务编辑打散；代价是模型得先想起来去读。将来若实测
  发现它想不起来，加注入不影响数据库那层。
- **页面这一版做到"开关 + 任务列表"。** 第 6 节把开关写成必须、把列表写成
  可以，两个都做了。列表是只读的：建任务、改状态都由模型调工具做，页面上
  再放一套按钮等于给同一件事开第二个入口，而那两套规则迟早会漂。

后续修订将 `agent` 与 task 完全解耦：删除 `agent` 的 `task_id` 参数和内部任务认领路径，子 agent 也不再获得 `task_read`。任务状态只由五个 `task_*` 工具管理。原先 `task_status(complete)` 不及时松开占用的问题仍由任务工具自身处理。

正文第 7 节提到的 `tools/todo.py` 等已随这一版删除；本节之前提到的
"每三轮一次的 todo 提醒"、`runtime_json` 里的 `todos` 字段、"待办装回来"
那条恢复路径，一并去掉了。旧库（v4 及更早）由第 5 条迁移升到 v5，只加表和
加列，历史消息和事件一个字不删。
