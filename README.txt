Git 仓库地址：待创建公开仓库后填写。

项目简介：
这是一个从零实现的轻量 Coding Agent Harness。项目不依赖 LangChain、LlamaIndex、OpenAI Agents SDK、AutoGen、CrewAI 等 Agent 框架，自行完成消息管理、工具定义、模型输出解析、循环终止、错误处理和本地执行。模型通过 config/llm.env 接入 DeepSeek、Qwen，以及通过 CloseAI 中转的 ChatGPT 和 Claude。

运行方法：
1. 将 config/llm.env.example 复制为 config/llm.env，填写 CLOSEAI_API_KEY 和 CLOSEAI_BASE_URL。ChatGPT/Claude 通过下拉框选择，Claude 还要根据中转协议设置 CLOSEAI_CLAUDE_PROTOCOL=openai 或 anthropic。
2. CLI：python run.py "Create a Python script and test it."
3. Web：python run.py --web --port 8767，然后打开 http://127.0.0.1:8767
4. 评测：python run.py --eval
5. 模式：CLI 使用 --mode code/ask/architect/context；Web 右侧直接切换。

核心能力：
- repo_map 提取 Python、C++、JS/TS 的文件、类和函数，按任务相关度与字符预算注入上下文。
- read_file 按行分段，search_files 搜索目录；不会整文件无界读取。
- replace_in_file 只替换唯一精确匹配的代码块；Python 候选代码语法错误时拒绝写入。
- code 模式可编辑、检查和运行；ask/architect 只获得只读工具；context 直接显示仓库地图。
- 工具结果过长时截断，早期步骤超预算时压缩，最近对话按预算保留；Web 显示上下文占用。
- Session 持久化到 .code_agent/sessions.sqlite3，保存消息、事件、结构化项目记忆和上下文 checkpoint；刷新网页或切换模型仍使用同一个 session。
- 工具调用记录 pending/running/completed/failed/interrupted 状态；服务重启后会把未完成调用标记为 interrupted，并注入恢复提示。
- Review 面板按 Agent 运行记录代码快照差异；勾选两次或多次修改后可以合并，工作区版本发生冲突时会拒绝写入。
- Monitor 面板记录 LLM 请求量、真实/估算 Token、Prompt 缓存命中率、平均/P95 延迟、LLM/Run/Tool 错误率、上下文压缩次数，并按模型、工具和最近错误分组。
- lint_file 支持 Python/C++ 语法检查；Run File 支持 Python 运行和 C++ 编译运行；静默命令也返回明确结果。
- 所有文件限制在 workspace 内，命令有危险模式拦截与超时。本地评测输出通过率和事件记录。

凭据：
config/llm.env 已加入 .gitignore。不要把 API Key 写入源码、README、提交记录或演示视频。

Session：
网页端首次发送请求会创建 session，浏览器用 localStorage 保存 session_id。左下角 Clear 会清空该 session 的消息、记忆、checkpoint 和工具状态；模型下拉框只切换本次请求的模型，不会创建新 session。

监控接口：
GET /api/monitoring?workspace=workspace 返回工作区聚合数据；追加 session_id 可只查看当前 Session。Token 优先使用模型 API 返回的 usage，没有 usage 的兼容模型会明确标记为 estimated，不作为官方计费值。
