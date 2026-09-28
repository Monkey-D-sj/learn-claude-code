# Agent 与 Bash 后台执行设计

状态：**设计稿，待实施（2026-09-28）**。适用于当前单进程、本机使用的 HTTP 服务。本文的 `job` 是一次后台工具执行；现有 `task` 是跨会话工作项，`turn` 是一次模型对话的执行记录，三者不能混用。

同日按现有代码逐条核对过一遍，改动集中在“初稿以为代码是这样的、实际不是”那几处：删掉了不存在的 `agent(task_id)` 前提，定下 `background_result` 的绑定方式、后台 turn 的来源落法和内部续跑的抢锁语义，补齐了 §7 接点表里漏掉的几件事。核对出来的代码现状约束都写进了对应段落，没有另开一节。

## 1. 目标与约定

- 只给 `agent` 和 `bash` 增加可选参数 `run_in_background: boolean`，默认 `false`。同步调用保持现有语义。
- 后台调用完成启动后，立即给原 `tool_use` 回填“正在执行”的 `tool_result`，带稳定的 `job_id`。主 agent 随即可以处理其他工作。
- 后台完成、失败或中断的结果必须交给主 agent。当前 turn 尚在运行时，在下一次模型调用前注入；当前 turn 已结束时，服务端自动创建一轮**后台结果续跑**，不等待下一条用户消息。
- 服务进程退出时立即退出，不等待后台工作结束。退出中的执行不自动重试；再次启动后标记为 `interrupted`，保留已知结果和可能已发生副作用的说明。
- 后台 job 归属启动它的会话。结果注入该会话的主 agent，不注入一次性子 agent 的上下文。`agent(prompt)` 的后台执行与 task 功能无关；会话关闭 task 时也必须可用。

第一版不提供跨服务重启继续执行、自动重试、工作区隔离或多进程分布式调度。后台执行不代表“可以安全并发修改同一文件”；工具描述应要求模型只把可独立推进的工作放到后台。

## 2. 工具协议

| 工具 | 参数变化 | 后台启动条件 |
| --- | --- | --- |
| `bash` | 增加可选 `run_in_background` | 命令通过现有 `PreToolUse` 权限检查，并已成功创建受控进程 |
| `agent` | 在必填的 `prompt` 之外增加可选 `run_in_background` | prompt 合法且后台执行已登记；与 task 开关无关 |

`run_in_background=false` 或未传时，返回值、权限检查和 120 秒 Bash 超时保持现有语义。参数为 `true` 时先完成权限检查和可持久化的启动登记，再启动 worker；启动失败直接返回错误，不能给模型一个实际不存在的 `job_id`。不要把“进入队列”说成“已经运行”：如果允许排队，状态必须明确为 `queued`。`agent` 只接受 `prompt`，创建 job 不读写 task 表，也不要求会话开启 task。

**后台 `bash` 是第二套实现，不是给现有那条路加一个开关。** 现在 `run_bash` 是 `subprocess.run(capture_output=True, timeout=120)`，`tools/bash.py` 自己的注释已经把两条路堵死：输出本来就全在内存里（`communicate()` 读到 EOF，切片只是切字符串），而 `TIMEOUT_SECONDS` 只管得住直接子进程 —— 孙子进程攥着管道，实测 `timeout=0.5` 遇到 `sleep 5` 要 5.1 秒才返回。§3 要的“边读边落盘”和 §4 要的 Job Object 在它上面都做不到，得换成 Popen + 读线程 + Job Object。120 这个数字沿用，但从此**同步和后台是两份代码共用一个常量**，不是同一份。

启动后的占位结果使用机器可读格式，例如：

```json
{"job_id":"bg_123","status":"running","tool":"bash","message":"后台执行中；结果完成后会自动通知。"}
```

这条占位结果是**原工具调用的唯一 `tool_result`**。完成通知不能再伪造一条没有对应 `tool_use` 的 `tool_result`。后台执行只使用独立的 `job_id`，`agent` 工具没有 `task_id` 入参。

增加一个只读的 `background_result(job_id, wait_seconds?)` 工具，供模型主动查询或在没有独立工作时等待。返回 `queued`、`running`、`completed`、`failed`、`interrupted`，完成时给出 `agent` 结论或 Bash 的退出状态与输出。`wait_seconds` 必须有短上限，避免模型密集轮询；结果自动注入仍是默认交付路径。主动读取到终态并成功进入会话上下文后，不再重复自动注入同一份结果。

## 3. Job 状态与持久化

job **不存进 `sessions` 行或会话上下文**；线程、进程句柄和 Windows Job Object 句柄也只存在当前进程内。`sessions.db` 中单独建一张 `background_jobs` 表，仅保存可恢复的元数据与结果索引：`id`、`session_id`、`source_turn_id`、`tool`、`status`、创建/启动/结束时间、错误、结果位置和通知状态。这里**没有 `task_id` 字段**；`session_id` 只用于确定结果该交给哪个会话的主 agent。job 的状态机不依赖 task 表。

**这是一次 schema 变更，`sessions.py` 的 `SCHEMA_VERSION` 要从 5 升到 6，`MIGRATIONS` 接一条**（`background_jobs` 用 `CREATE TABLE`，不用重建任何现有表）。

**`session_id` 外键不能照抄 `events` / `turns` 的 `ON DELETE CASCADE`。** 那条路会在“结果还没交付就把会话删掉”时让结果**静默消失**。要么不挂 CASCADE（留下孤儿行，由清理逻辑收拾），要么在 `_post_delete` 里显式处理 —— 见 §7 的 `server.py` 一行。无论选哪条，“job 元数据和 §3 说的结果文件要一起清理”这件事都得有个明确的所有者。

这张表解决两个跨 turn 的问题：原 turn 结束后，完成结果仍要自动触发该会话续跑；结果已经生成但尚未注入时，进程退出不能让它凭空消失。重启时也靠它辨认哪些执行被截断。它**不用于恢复或继续运行中的 worker**。状态迁移为：

```text
queued → running → completed | failed | interrupted
```

`queued` 可以在 worker 已预留、尚未真正开始时使用；若实现选择“容量满时拒绝启动”，则可省去队列和该状态。并发量必须有上限，容量满时返回明确错误或 `queued`，不能无限创建线程。状态变更用短事务，模型调用和 Bash 执行期间不持有 SQLite 事务。

结果正文可能很大。Bash 输出应边读取边写入受控文件或做有界缓存，数据库存元数据及结果位置；给模型和页面的完成通知只含状态、必要摘要及可取回完整结果的引用。不能让多个后台 Bash 的完整输出长期堆在内存里。结果文件的位置必须在当前工作区可管理范围内，并与 job 元数据一同清理。

每个 job 有独立的通知状态（例如 `pending` / `delivered`）。结果先持久化，再唤醒会话。通知进入 `messages`、原始消息记录和 checkpoint 后才标记 `delivered`；崩溃重试用 `job_id` 去重，不因一次事件发送失败丢掉结果。事件是页面展示通道，不能代替数据库里的 job 状态或会话上下文。

## 4. 执行与进程退出

后台 `agent` 用不阻止解释器退出的 daemon worker。普通 `ThreadPoolExecutor` 不符合“服务立即退出”的要求，即使 `shutdown(wait=False)`，运行中的任务仍会阻止解释器退出。worker 内显式建立所需的会话、turn、用量和工具上下文；不能假设调用线程的 `contextvars` 会自动传给新线程。

后台 `bash` 由 daemon worker 管理，Bash 及其后续进程放入 Windows Job Object，并设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`。取消或超时时终止整个 Job；服务进程退出时，句柄关闭使其中的进程结束。启动 Bash 与加入 Job 之间不得留出能派生未受控后续进程的窗口。单独 `Popen.kill()` 只处理直接进程，不能作为进程树清理方案。现有 120 秒命令超时仍适用，超时结果必须保留已完成的部分输出并标注副作用可能只执行了一部分。

daemon worker 会被进程退出直接截断，不能依赖其 `finally` 写终态。服务再次启动时，在接收新请求之前核对旧进程留下的 `queued` / `running` job，将其标为 `interrupted`，不自动重启工具。后台 job 不改变 task 状态；task 的中断和重试仍按独立的任务系统处理。

**启动顺序要和 `reap_running` 排清楚。** 现在是 排他锁 → 迁移 → `reap_running()`（把所有 `running` 的轮收成 `interrupted`）→ 开始收请求（`server.py` 的 `__main__`）。job 核对接在 `reap_running` **之后**、收请求**之前**。

重启核对产生的 `interrupted` 结果同样进入完成通知队列，不能只改数据库状态而让主 agent 永远不知道。**但交付路径要看那一轮的轮是什么下场**：如果它的轮也被 `reap_running` 收成了 `interrupted`，那就不许在启动时单独起一轮续跑去送它 —— 那会撞上 §5 的恢复闸门，而且一次就够让那个中断轮**永久**不能恢复（见 §5）。它保持待交付，等用户点继续或放弃时，由那条路的检查点 1 顺带注入。只有那一轮在进程退出前已经正常收尾（job 比它的轮活得久）的情况，才走自动续跑。

任务完成、失败与服务中断都要给页面可读状态；服务退出本身不等待写完所有 job 的终态，重启核对承担这一步。会话删除必须先处理其活动 job 的归属，不能让 worker 随后把结果写回已删除会话 —— 机制见 §7 的 `_post_delete` 一行。

**用量归属要显式重建，这不是顺手的事。** 后台 `agent` 的 worker 是新线程，而 `usage.span(session=…, turn=…)` 和 `bind_recall` 都是 contextvar（`server.py` 的 `_drive` 里绑的）。不在 worker 里重新建一层，这笔钱会变成一条**没有 session / turn 的孤儿记录**。`tools/subagent.py` 为同一件事专门写过一段注释，而后台 job 是它的加强版：嵌套、没人看、没人问，只被回合上限拦着。

## 5. 完成通知与自动续跑

主 `agent_loop` 在完整消息边界、`compactor.prepare()` 与 checkpoint 之前，取出该会话新完成的 job，按完成时间和 `job_id` 稳定排序，合并成一条明确标记的内部消息。只有终态变化才通知；每轮不重复塞“仍在运行”。通知示例：

```text
[background_results — 服务端事件，并非用户输入]
job_id: bg_123
tool: bash
status: completed
exit_code: 0
output: ...
```

Anthropic 消息只有 `user` / `assistant` 角色，因此该内部事件在协议上是一条 `user` 文本消息，但**存库的 `kind` 用 `control`，不能用 `user_input`** —— 页面见到 `user_input` 就按“你说的”原样画出来，而 `control` 本来就是“协议里的东西、不是对话内容”那一档（`ui/index.html` 的 `renderMessage` 里那条注释说的正是这件事），旁注形式正好。不要修改已经完成的旧 turn，也不要追加孤立的 `tool_result`。

检查点有两处：

1. 每次模型调用前检查，保证活跃 turn 能看到已完成结果。
2. 模型没有再发工具调用、准备返回最终答复时再检查一次。若此时有新结果，注入并继续调用模型；若检查之后才完成，由完成事件唤醒后续流程。

当原 turn 已经结束，job 完成事件由服务端调度器发起新的**内部续跑 turn**：取得该会话锁，读取最新上下文，创建来源为 `background` 的 turn，注入完成通知，运行与普通 turn 相同的模型循环、记录和收尾路径。此 turn 不触发 `UserPromptSubmit`（`_drive(first_time=False)` 已经支持），不伪造用户提问。多个相近时间完成的 job 尽量合并到同一续跑 turn。

**“来源”落在哪儿，这里定下来。** `turns` 表没有来源列，而 `turn_messages.kind` 的 CHECK 只有上面那四种。加第五种 kind 就要**重建 `turn_messages`**，而 v4 重建 `turns` 的坑已经踩过一次：开着外键 `DROP TABLE` 会顺着 CASCADE 把**所有原始消息删光**，而且不报错，必须走 `NEEDS_FK_OFF` 那条专门的路。为一个标记付这个代价不值。

所以：**`turns` 加一列 `source`**（`'user'` / `'background'`，默认 `'user'`，`ALTER TABLE ADD COLUMN` 就够，不重建表），页面按它给整轮标来源。

**不能直接拿 `begin_turn` 建这一轮。** 它写死第一条消息是 `kind='user_input'`（页面就按“你说的”画，正好是上面要避免的），而且会把会话标题改成这条通知的开头。要一个同事务的兄弟入口：写 `kind='control'`、`source='background'`、**不碰 `sessions.title`**。

若用户 turn 正在运行或会话锁被占用，完成结果保持待交付，不能另起并发模型循环。**但“现有 turn 结束并释放锁后再调度”不能只靠完成事件触发**：`agent_loop` 有好几条出口完全绕过上面那两个检查点 —— 回合上限、额度耗尽、输出撞 `max_tokens`、API 错误（`agent.py` 里那几处 `return TurnOutcome`）。job 恰好落在这些路径上完成就会滞留，而 `pending` 状态没有别的东西会再来推它。所以 **turn 收尾时必须再扫一次待交付**。

**跟用户请求撞上时是“当场拒绝”，不是“排队随后处理”。** `_post_ask` 用的是 `lock.acquire(blocking=False)`，拿不到立刻回 409 `this session already has a turn running`，用户得自己重发。内部续跑沿用同一条规矩：不新开一条会挂住 HTTP 线程几分钟的路，也不去改 `_post_ask` 的阻塞语义。初稿写的“取得锁的一方先处理，另一方随后读取最新上下文”跟代码不符，这里改过来。不能靠短间隔忙轮询等待锁。

已有未解决的 interrupted turn 或 `UNSAVED` 结果时，不得绕过现有恢复闸门启动内部续跑；job 结果保持待交付并在页面说明等待恢复处理。**这条闸门比初稿说的更硬**：`checkpoint_info` 有一条 `turn_no != last_no` → `has_later_turn` 的判据，所以哪怕只有一轮后台续跑从中间插进去，那个中断 turn 就**永久**不能再恢复了 —— 不是“等一下还能续”，是“再也续不了”。

而且这道闸现在是**内联在 `_post_ask` 里**的（`_flush_unsaved` → `unresolved_interrupt` → `_run_turn`），`_post_resume` / `_post_abandon` / `_post_tasks_enabled` 各抄了一部分。内部续跑要受同样约束，得先把它抽成一个公共入口，不能再抄一遍。

自动续跑不能依赖已经关闭的 HTTP 响应流。模型输出、工具调用、权限问题和最终答复都必须落入现有会话事件/轮次记录，再由页面增量读取。

**这件事比初稿估的轻 —— 大半是现成的。** `recording_emit` 本来就是先 `STORE.append_event` 再进流，`/turns` 已经把 `PENDING` 里挂着的提问补回给页面，`/answer` 认的是 `rid`、不需要知道原来那条流，`ASK_TIMEOUT` 也照旧生效。真正要改的只有一处：内部续跑没有 `wfile`，`_ask_and_wait` 靠 `emit_quietly` 的返回值判断“还有没有人能回答”，而喂给它一个空 emit 时它会**立刻**返回“流已经断了，没人能回答”。续跑那条路必须把这个判断改成“没人直播但在等页面来读”，否则权限确认会被静默拒掉 —— 那正是这一节要避免的。超时仍按现有规则处理。

## 6. 页面与会话行为

- 占位结果出现时，页面展示 job ID、工具和运行状态；完成、失败、超时、中断时更新同一 job，而不是只追加一句散落的日志。
- 页面当前在 `running=false` 时停止轮询；有未完成或待交付的 job 时仍需保持增量读取，才能看到完成事件和自动续跑 turn。会话列表与轮次接口应返回必要的后台状态，刷新后可重建页面。
- **但这个状态不要塞进 `running` 字段。** 它是 `is_running()`，也就是 `lock.locked()`，被三处接口发出去（`/sessions`、`/turns`、`/events`），而页面拿到它第一件事是 `setComposer(!data.running)` —— 把“有 job 待交付”算进 `running`，输入框就被锁上了，正好跟上一条“用户可以发新消息”打架。另给一个独立字段（例如 `jobs_pending`）：页面拿它决定**要不要继续轮询**，`setComposer` 仍旧只看 `running`。
- 后台 job 不占用整段会话锁，用户可以发新消息；同一时刻仍只允许一个主 agent turn。后台 worker 对工作区的改动可能与前台操作并发，工具说明要明确避免同时改同一文件；现有文件锁只能保护通过对应工具写入的路径，不能保护 Bash 任意命令。
- 自动续跑可能继续调用需要确认的工具。页面应展示该续跑 turn 的挂起提问，回答仍走现有 `/answer`；用户不在时按超时规则返回，不默认批准。

## 7. 与现有实现的接点

| 文件 | 需要处理的接口 |
| --- | --- |
| [`tools/bash.py`](../tools/bash.py) | `run_in_background` 参数、受控进程树、超时和输出收集。**第二套实现**，见 §2：`subprocess.run(capture_output=True)` 上做不了有界输出和进程树终止，要换 Popen + 读线程 + Job Object |
| [`tools/subagent.py`](../tools/subagent.py) | `agent(prompt)` 的后台参数与 job 归属；不接入 task 状态机。`background_result` 要加进 `_DENIED` —— 结果只回主 agent，给子 agent 一个能查 job 的工具等于开第二条口子 |
| [`tools/__init__.py`](../tools/__init__.py) | 注册 `background_result`。**它不能是模块级单例**：这个文件的规矩是“除 ask 外都是无状态的，可以全进程共用一份”，而它必须知道“我是哪个会话”，`agent_loop` 又只给 handler 传 `**block.input`。走 `recall` 那条现成的路 —— 留在 `BASE_TOOLS`，每轮 `bind` 一个会话 contextvar。**不要走 `per_turn`**：那条路要同时在 `server.py` 的 `signature_tools()` 里补一遍，漏了的话所有检查点都恢复不了。子 agent 里的后台 Bash 归属主会话，无法确定归属时拒绝启动 |
| [`agent.py`](../agent.py) | 完整回合边界的注入、最终答复前复查、占位工具结果与完成通知的区分。注意那几处绕过检查点的出口；收尾的待交付重扫在 `server.py` 那一侧 |
| [`sessions.py`](../sessions.py) | job 表（**`SCHEMA_VERSION` 5→6**）、状态迁移、通知去重、`turns.source` 列与后台开轮入口（**不能复用 `begin_turn`**）、启动后的中断核对 |
| [`server.py`](../server.py) | worker 与完成调度、会话锁下的内部续跑、无需直播流的 emit / ask、退出行为。**比初稿写的重**：闸门要从 `_post_ask` 里抽成公共入口；`_post_delete` 现在只抢锁、查存在、`delete_session`，完全不认识 job，而 `events` / `turns` 都是 `ON DELETE CASCADE`；`is_running` 的语义不要改（见 §6）；`__main__` 里核对与 `reap_running` 的先后；worker 里显式重建 `usage.span` |
| [`ui/index.html`](../ui/index.html) | job 状态展示、后台续跑 turn、空闲期间轮询及挂起提问。轮询看新加的 `jobs_pending`，`setComposer` 仍旧只看 `running` |

现有 `PreToolUse` 必须在后台启动前完成；不能把权限确认推给无人交互的 worker。后台启动的占位结果不触发代表“工具执行完成”的 `PostToolUse`；worker 真正结束后，以原调用信息和最终输出触发一次。这样依赖最终输出的 hook 不会把启动确认误判为完成。工具集变更会改变 checkpoint 恢复签名，部署前后仍按现有签名校验处理未解决的中断 turn，不能强行恢复旧格式。

## 8. 验收要点

1. `bash` / `agent` 默认同步；显式后台时原 `tool_use` 很快收到含 `job_id` 的占位结果，主 agent 可以继续调用其他工具。
2. 未通过权限检查、容量已满或启动失败时，不产生虚假的 `running` job；已有副作用与错误状态能被区分。
3. 活跃 turn 在下一次模型调用前收到完成通知；即将结束时的完成结果也能被最终复查捕获。已结束的 turn 通过新内部 turn 自动续跑，无需用户再发消息。
4. 同一 job 在保存的上下文中只交付一次；主动查询、多个 job 同时完成、会话锁竞争、数据库写入失败与进程重启均不造成结果静默丢失。
5. 关闭服务不等待 daemon worker；Windows 上 Bash、其后续进程在 Job 关闭后结束。重启后运行中的 job 显示为 `interrupted`，不自动重跑。
6. task 开关开或关，`agent` 都只有 `prompt` 入参；后台执行不认领、不创建、不修改 task。
7. 页面在原 turn 结束后仍能展示 job 完成与自动续跑；刷新后状态一致。内部续跑需要权限或用户回答时，可以显示问题并通过 `/answer` 回答。
8. 后台 Bash 的大输出不会无限堆内存；模型拿到退出状态、必要摘要和可取回的完整结果。

验证使用临时数据库与受控的短命令、短子代理替身，不碰当前 `sessions.db` 或用户正在执行的命令。测试应覆盖服务退出后的状态核对和进程树终止，不只验证参数与占位字符串。
