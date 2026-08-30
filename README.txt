Git 仓库地址：待创建公开仓库后填写。

项目简介：
这是一个简化版 coding agent / harness demo，目标是展示编程智能体的关键闭环：维护对话历史，把本地工具以 OpenAI 兼容 tool calling 格式交给模型，解析模型的 tool_call，在受限工作区内读写文件和执行命令，再把工具结果写回上下文，直到模型调用 finish 或达到最大轮数。项目未使用 LangChain、LlamaIndex、OpenAI Agents SDK、AutoGen、CrewAI 等 agent 框架。

如何运行：
1. 命令行运行：
   复制 config/llm.env.example 为 config/llm.env，填写 CODE_AGENT_API_KEY、CODE_AGENT_BASE_URL、CODE_AGENT_MODEL。
   python run.py "Create a Python hello world script and run it."

2. Web 产品 demo：
   python run.py --web
   浏览器打开 http://127.0.0.1:8765

特色功能：
- 本地工具自行实现：list_files、read_file、write_file、run_command、finish。
- 路径防护：所有文件操作限制在 workspace 内。
- 命令保护：拦截部分明显危险命令，并限制执行超时。
- 真实模型驱动：通过 config/llm.env 接入 OpenAI 兼容网关或 DeepSeek 等模型服务，不保留固定脚本生成代码。
- Web 页面采用类似 Codex/VS Code 的工作台布局：左侧文件列表，中间是可编辑、可保存、可运行的代码编辑器，右侧是 agent 对话窗口，底部用户输入栏用于派发编程任务；界面隐藏工具调用过程，只保留用户消息、assistant 回复、最终结果和错误。

凭据说明：
API key 写在 config/llm.env 或系统环境变量中；config/llm.env 已被 .gitignore 忽略，不要提交到仓库、README 或视频中。
