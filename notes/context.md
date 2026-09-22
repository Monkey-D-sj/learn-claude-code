# 上下文管理

> **状态:原始设计稿,不是现行设计。** 写在动手之前,只当历史读。哪些落地了、
> 哪些变了,对着下面那张表看;**以代码和 `README.md` 的"上下文压缩"一节为准**。
>
> | 设计稿里这条 | 现在是什么样 |
> |---|---|
> | 工具输出压缩:落盘 + 裁剪,head 2k、tail 也带上 | 落地为第 1 档 `tool_result_budget`,`PREVIEW_HEAD=2000 / PREVIEW_TAIL=300` |
> | snip 压缩:轮次多了压前面 | 落地为第 2 档 `snip_compact`(阈值 150 条),**当前被注释掉** |
> | 阈值:开头留 3、末尾留 30,中间强制压 | `SNIP_HEAD_MESSAGES=3` + `KEEP_RECENT_RESULTS=30`,按**结果个数**保,不是条数 |
> | 大模型决定:每轮给自增 id + token,模型自己点一段压 | 落地为 `compress` 工具,号(`m00007`)拼在每条工具结果末尾;**不是**"铺平成字符串"给模型 |
> | 摘要:剥离思考块 | **没做,而且是反的** —— `estimate_tokens` 把 thinking 照常算进来(实测进了 prompt 就进账) |
> | 摘要那档"阈值触发" | 落地为第 4 档 `compact_history`,**当前被注释掉** |
> | Anthropic 有 `cache_control` 能让服务端删缓存,DeepSeek 不支持 | DeepSeek 的缓存是**自动**前缀缓存,不需要打点;所以压缩宁可少压、压得狠,别频繁浅压 |

## 核心

- 减少上下文
- 尽可能少破坏缓存，也就是尽可能保留前缀

## 策略

### 工具输出压缩

比如说 Read（读大文件）、Terminal（命令产出长堆栈），会产生很多输出，
这时候要先落盘完整输出，方便大模型后续查看。然后裁剪输出，比如说Read提供head 2k，
Terminal因为会产生报错/结果之类的信息，最好把tail也带上  
例子：  
<persistent-output>  
output saved at {path}  
preview output {***}  
</persistent-output>

### snip压缩

当轮次多了之后，对前面的工具结果进行压缩
Anthropic自家模型有cache_control可以让服务器删缓存，不破坏缓存前缀，其他的比如说
deepseek不支持

### 摘要总结

#### 大模型决定

每轮消息提供自增id + token 消耗，大模型可以根据id，判断当某个阶段完成，对后续
无用时，压缩比如说 3-20 的例子


#### 阈值

决定，比如说开头保留3，末尾保留30，中间的强制压缩并替换
给大模型的内容：轮次消息铺平成一个字符串，剥离思考块
例如：
[USER]: 帮我把重试逻辑改一下                                                                                                                         
[ASSISTANT]: 我先看下现状。                                                                                                                        
[Tool calls:                                                                                                                                       
terminal({"command": "grep -rn retry gateway/"})                                                                                                 
]                                                                                                                                                
[TOOL RESULT call_abc]: gateway/run_turn.py:412:    retries = 3
