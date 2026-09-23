# 会话功能：先实现一个会话内的主流程

状态：第一阶段已实现（sessions.py v2 迁移、turns / turn_messages /
session_contexts、`GET /session/{id}/turns`、页面的轮次展示）。
本文其余部分保持实施前的写法，作为这次改动要满足的规格。

落地时有三处和原文不同，都是实现时才看出来的：

1. **确认的结果记进轮次**。原文的 Message 四种 kind 里没有"权限确认"这一
   种，而 ask 又不是一条消息——库里不写的话，刷新之后这一轮的记录里完全
   看不出"它当时问过你、你是怎么答的"。所以 `make_ask` 在给出结果时补一条
   `control` 消息（`[permission] ... → 已允许`）。不新增表，也不占用
   turn_messages 之外的任何东西——权限的完整持久化仍留给后续增量。
2. **页面的去重靠一个游标**。`/turns` 里额外返回 `cursor`（和那份 turns
   读自同一个快照的最大事件 seq），页面拿它当起点，只画之后的新事件。
   没有它就只能两边都画、再写一套"这条事件对应哪条消息"的去重规则。
   唯一补不回来的是挂起中的确认，所以 `/turns` 里还带一个 `pending`——
   它必须是**先落库再发事件**（agent.py 的 record 排在 emit 前面）才成立，
   否则游标划过去的那条消息可能还没写进 turn_messages。
3. **`last_compacted_at` 由压缩器回调触发**，不是从日志里看的。`prepare()`
   多收一个 `checkpoint` 参数，靠比压前压后的序列化来判断"这一档到底改了
   没有"——四档各自知不知道不重要，判断只写在一处。

本阶段包含 Session、Turn、Message，以及保存模型工作上下文的 SessionContext。目标是在一个会话里发起请求、执行工具、保存结果，再接着发起下一轮。先完成正常使用的流程，后面逐步增加取消、恢复、去重与并发边界。

## 1. 本阶段要跑通什么

```text
创建 Session
    ↓
用户发送第一条请求
    ↓
创建 Turn 1（running）并保存用户 Message
    ↓
模型响应 → 工具结果 → 模型继续响应
    ↓              每一步保存到 Turn 1 的 Messages
Turn 1 结束（completed，普通错误则 failed）
    ↓
用户发送第二条请求
    ↓
创建 Turn 2，带上这个会话已有的上下文继续执行
```

一个 Turn 是一次用户请求到执行结束的过程。模型在内部请求五次 API，仍然属于同一个 Turn。

本阶段沿用现有 HTTP 线程执行和会话锁，一次发送一条请求，等它结束再发送下一条。

## 2. 最小数据模型

### Session：聊天空间

沿用现有 sessions 表：

| 字段 | 含义 |
| --- | --- |
| id | 会话 ID |
| title | 标题 |
| created_at | 创建时间 |
| updated_at | 最近聊天活动时间 |

### Turn：这一轮执行

新增 turns 表：

| 字段 | 含义 |
| --- | --- |
| id | 轮次 ID |
| session_id | 所属会话 |
| turn_no | 会话内的轮号，1、2、3…… |
| status | running / completed / failed |
| created_at | 创建时间 |
| updated_at | 本条记录最后修改时间 |
| finished_at | 结束时间，执行中为空 |
| error_message | 普通执行错误的原因，可空 |

创建即开始执行，所以本阶段 created_at 也作为开始时间。以后有排队阶段时再加 started_at 区分接收和执行。

状态机只有：

```text
running → completed
running → failed
```

会话内按 turn_no 排序；updated_at 在状态、错误信息变化时更新，不改变轮次顺序。

加上 UNIQUE(session_id, turn_no) 和 session_id 外键。创建 Turn 时在一个短事务中查询 MAX(turn_no) + 1，再插入记录和用户消息。本阶段不删除单个 Turn，不重新编号。

### Message：这一轮的内容

每条 Message 归属一个 Turn，通过 Turn 找到 Session：

| 字段 | 含义 |
| --- | --- |
| id | 消息 ID |
| turn_id | 所属 Turn |
| message_no | 这一轮内的消息顺序 |
| kind | user_input / assistant_response / tool_result / control |
| role | 当前模型协议中的 user / assistant |
| content_json | 内容块 JSON，保存文字、工具调用、工具结果 |
| created_at | 创建时间 |

加上 UNIQUE(turn_id, message_no) 和 turn_id 外键。一轮里的消息按 message_no 排序，整个会话按 turn_no、message_no 排序。

当前 Anthropic 协议的工具结果 role 也是 user，所以通过 kind 区分真正的用户输入和工具返回。tool_call_id 保存在 tool_use/tool_result 内容块中，本阶段不必额外增加工具执行表。

例如：

```text
Turn 1
  Message 1：user_input         “看看 README”
  Message 2：assistant_response tool_use(read_file, id=c1)
  Message 3：tool_result        tool_use_id=c1，README 内容
  Message 4：assistant_response “这个项目主要用于……”
```

### SessionContext：保存压缩后的工作上下文

新增 session_contexts 表，每个 Session 一条记录：

| 字段 | 含义 |
| --- | --- |
| session_id | 主键、外键，对应 Session |
| messages_json | 当前提供给模型的完整消息列表，包含已生成的摘要及保留的消息 |
| version | 每次成功保存快照后加一，从 1 开始 |
| updated_at | 最近保存上下文的时间 |
| last_compacted_at | 最近实际压缩的时间，尚未压缩时为空 |

这就是讨论中的“压缩信息表”。它保存当前有效的模型上下文，也包含尚未压缩过的上下文；不是每次压缩都新增一条历史日志。一个会话保留一份最新快照即可。

```text
Turn → Messages：原始输入、响应和工具结果，追加保存
Session → Context：供模型继续使用的工作副本，可以压缩和替换
```

压缩只改 Context，不改原始 Messages。messages_json 必须是可继续发给模型的完整消息列表，不能只存一段摘要文本；当前用户请求、近期消息和必要的工具协议结构都需要保留。

保存规则：

1. 创建会话时保存空列表，version=1。
2. 新一轮从 Context 加载消息，在内存中追加本轮输入和执行结果。
3. 每次实际压缩后，在工具调用与结果完整配对的位置保存快照，更新 version、updated_at、last_compacted_at。
4. 一轮结束后再保存最终快照并更新 version、updated_at；未压缩时不改变 last_compacted_at。
5. 不将同一批原始 Messages 再追加到已包含它们的 Context。第一版直接沿用本轮内存消息列表，不实现从数据库游标增量重建上下文。

快照更新与 Turn 正常结束放在同一短事务中，避免正常提交时出现“本轮已完成但下一轮读取的仍是旧上下文”。普通失败时保留现有的未配对工具消息整理逻辑，保存可继续使用的上下文。

压缩逻辑可通过返回标志或回调告知调用方是否实际发生压缩；不通过截断后的页面日志猜测。version 表示快照版本，不表示压缩次数。本阶段不增加压缩历史、token 统计、自动恢复检查点等功能。

### 与当前 messages 表怎么衔接

当前 messages 表存的是压缩后的模型上下文快照，会整体替换。不能直接给它加 turn_id 就当作上述原始 Message：摘要可能同时包含多轮内容。

本阶段把现有 messages 中各会话的快照按原顺序迁入 session_contexts.messages_json，并将上下文读写切换到新表。旧快照是否曾压缩无法可靠判断，last_compacted_at 留空，不编造历史时间。

新增 turn_messages 表保存上述原始 Message，暂用这个物理表名避免与旧表冲突。旧 messages 停止读写，迁移验证前保留作备份；不在本阶段同时重命名所有表。旧页面历史仍由已有 events 展示，不伪造旧 Turn，也不把压缩快照当作完整原文。

因此，本阶段新增 turns、turn_messages、session_contexts，继续使用 sessions 和已有 events；旧 messages 的上下文职责由 session_contexts 接替。迁移在停服备份后的数据库副本先验证，确认原有会话可继续聊天，再用于实际数据库。

## 3. 一轮请求如何实现

继续使用现有 POST /ask，不先引入新的任务提交协议。

1. 读取 session，取得现有会话锁，从 session_contexts 加载已有模型上下文。
2. 创建 running Turn，分配 turn_no；同时保存这轮用户 Message。
3. 将用户输入追加到内存工作上下文，调用现有 agent_loop；实际压缩后按上述规则保存 Context。
4. 每次完整模型响应回来，追加一条 assistant_response 到 turn_messages。
5. 每个工具完成，追加一条 tool_result。模型可以继续调用工具，记录始终关联当前 turn_id。
6. 模型最终回复只保存一次；若 Stop hook 要求继续，则这轮还未结束。
7. 在同一短事务里保存最终 Context、更新 Turn 为 completed，并填写 finished_at、updated_at。
8. 普通 API 错误、轮数耗尽或捕获的异常，使 Turn 进入 failed，保存错误原因；工具报错但模型仍可处理时不立即结束整轮。

所有数据库事务保持短小，不包住模型请求和工具执行。

注意：当前 agent_loop 会把一些错误作为字符串返回。实现时让它返回简单的结构化结果，至少区分成功、失败、最终文本和错误原因，不能通过字符串是否以 Error 开头来判断终态。

模型消息保存使用单独的记录回调，例如 record_message(kind, role, content)，由服务端绑定 turn_id。不要从已经截断的页面 Event 反向恢复原始 Message，也不要在轮末对压缩后的 history 做切片来猜测哪些消息属于这一轮。

## 4. 页面先显示什么

在当前会话里能看到：

- 第 1 轮、第 2 轮等编号。
- 每轮的用户输入、回复和工具记录。
- 当前轮的运行中、完成或失败状态。

增加只读接口 GET /session/{id}/turns，返回按 turn_no 排序的 Turn 及其按 message_no 排序的 Messages，供查看和刷新后重建轮次展示。先服务于小规模 demo，不在本阶段做复杂分页或多客户端增量协议。

当前 NDJSON 与 events 继续服务实时过程和旧历史。给现有轮次内展示通知附带 turn_id 即可定位新的轮次容器；刷新后新轮次从 turns 接口读取，不再重复绘制同一批 Event。旧版记录放在旧历史区域，只使用原来的重放路径。

Events 在这里是现有界面的兼容机制。本阶段不扩充完整事件体系，也不安排删除它的重构。

## 5. 实现涉及哪些文件

| 文件 | 本阶段改动 |
| --- | --- |
| sessions.py | 增加 turns、turn_messages、session_contexts；迁移旧快照，提供轮次、原始消息和上下文读写 |
| server.py | 在已有请求流程里创建 Turn，传入记录回调，收尾保存状态；提供轮次查询 |
| agent.py | 在完整模型响应、工具结果、控制消息产生时记录内容；返回可区分成功与失败的结果 |
| ui/index.html | 在一个会话内展示轮次编号、状态和消息 |
| context.py | 沿用现有压缩算法，报告实际压缩；工作副本保存到 Context，原始轮次消息由独立回调保存 |

时间字段本阶段沿用现有库的 UTC Unix 秒（REAL），避免在同一最小改动里混用时间单位。完整参考方案的毫秒字段不直接用于本阶段迁移；以后如需统一，单独迁移。

## 6. 完成标准

1. 创建一个会话，发送请求后立即出现 Turn 1，状态 running。
2. 模型响应和工具结果都能找到对应的 Turn，消息顺序正确。
3. 执行结束后，Turn 1 变为 completed，带结束时间。
4. 同一会话继续提问，创建 Turn 2，并能使用前一轮的模型上下文。
5. 正常完成后刷新页面，轮号、内容和终态仍然存在。
6. 一次普通模型请求失败后记录 failed 和原因，下一轮仍可发起。
7. 触发一次压缩后，原始轮次 Messages 不被改写；Context 保存压缩结果，下一轮能直接使用。
8. 保存普通未压缩快照只增加 version 和 updated_at；last_compacted_at 只在实际压缩时变化。
9. 旧会话的模型快照迁入 Context 后，仍能继续聊天，旧版展示历史仍然可读。

用假的模型验证两轮正常执行、一次工具调用、一次普通失败与一次压缩，并用临时数据库验证旧快照迁移。中断、重复发送和跨会话竞争不作为本阶段验收条件。

## 7. 后面逐项增加

取消、服务重启后的 interrupted 状态、自动恢复、请求去重、并发删除和切换竞态、后台队列、授权持久化，均放在后续增量中。沿用已有防护，不为了简化主流程拆除它们。

本阶段中途关闭服务，未结束 Turn 可能仍显示 running；这是暂未实现启动恢复的明确限制。原始工具记录也不代表能够安全自动重跑。

[完整会话设计](session-design.md) 只用于查阅后续能力。当前实施范围以本文为准：先完成一个 Session 内，Turn 与 Message 的创建、执行、保存、查询，并通过独立 Context 保存压缩后的上下文以继续聊天。
