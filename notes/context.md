# 上下文管理

## 核心

- 减少上下文
- 尽可能少破坏缓存，也就是尽可能保留前缀
- 不遗忘以前过程

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
