# 架构设计

```text
Web 控制台 / CLI
 └─ AgentHarness
     ├─ 兼容 OpenAI 协议的模型适配器
     ├─ ContextManager ── 压缩后的提示词 + 持久化原始轨迹
     ├─ ToolRegistry ── PolicyEngine ── Hooks
     │   ├─ 读取 / 列表 / 搜索
     │   ├─ 精确替换 / 创建文件 / Git diff
     │   ├─ 项目 Skills
     │   └─ DockerExecutor 或 LocalExecutor
     ├─ SessionStore ── SQLite + JSONL + 大输出产物
     └─ IndependentVerifier
         ├─ 干净 Git 克隆
         ├─ 应用候选补丁
         ├─ 注入保护测试
         └─ 全新执行器与验收命令
```

Web 控制台是仅监听回环地址的控制器，基于 Python 多线程 HTTP 服务器构建，并打包静态 HTML/CSS/JavaScript。浏览器请求用于创建或检查持久化会话；实际 Agent 任务在控制器后台线程中执行，并可通过 SQLite 事件持续观察。密钥写入控制器的 `.env`，在 API 响应中被隐藏，并且绝不会注入 Docker。

Git 仓库使用分离式 Worktree。对于普通文件夹，控制器会先在 `sources/<session>` 下创建受管副本，排除本地密钥和依赖缓存，初始化私有 Git 基线，随后复用相同的 Worktree 与验证流程。

模型不能自行决定任务已经通过。`submit_result` 只是一项完成请求。运行时会创建干净验证检出、应用补丁、在 Agent 工作区之外加入保护测试，并执行固定命令。验证失败会作为新的工具结果反馈给模型。

每个工具调用都会在执行前记录为“已开始”，并在执行后标记为“已完成”。恢复时，不完整记录会显示为“不确定”，且不会被自动重放。仓库状态仍可供检查。下一次请求模型之前，运行时还会为缺失的工具消息插入明确的合成结果，在不重放副作用的前提下保持请求/结果配对。

上下文压缩只改变后续轮次发送给模型的提示词。完整的模型与工具事件流仍保存在 SQLite 和 `events.jsonl` 中；大体积输出会保存为产物，并在对话中提供首尾预览。
