# ADR 0020 · 结构化模型决策与续作

- 状态：已采纳并实现
- 日期：2026-11-22

## 背景

11 月 22 日现场的第三步返回了简短说明和一个合法的 `read_file` JSON。执行器整段解析失败，多发了一次模型请求；后者只留下笼统错误，原任务带安全断点却一次都没自动重试。前台同一智谱接入能正常回复。历史 31 次读超时各开了新会话，119 条熔断记录并非请求。全局拉高超时、换供应商或靠提示词加重「只输出 JSON」不能解决这条链路。

## 取舍

- ZCode 的 [工具循环](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/design/v2/loop/tool-calling-loop.md)、[流中断恢复](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/design/v2/model/stream-error-retry.md) 以及 Codex 的 [传输层](https://github.com/openai/codex/tree/main/codex-rs/codex-client) 和 [类型化 API 层](https://github.com/openai/codex/tree/main/codex-rs/codex-api) 都把协议接收与工具执行分开。采用流、结构化决策和完成边界；不照搬上游的依赖、重试次数或流中提前执行策略。
- 新增可选的模型接入「执行决策」模式：自动、结构化流式、兼容文本。自动只对已知端点和模型启用；不支持或行为不明的网关不猜测能力。模式不影响对话、目标生成、摘要、语义验收等文本调用；不改用户选的模型、凭据和 Base URL。
- 在原生 function/tool-use 通道只声明一个 `proto_decision` 决策信封，复用 Proto 的工具清单、参数校验和审批。每步最多一个动作；不维护跨供应商的工具调用 ID 历史，继续把有界的本地回执作为下一步的输入。完整流收齐并校验前不派发工具，不在流里拼接重试结果。
- [智谱流式消息](https://docs.bigmodel.cn/cn/guide/capabilities/streaming)、[工具流式输出](https://docs.bigmodel.cn/cn/guide/capabilities/stream-tool)和 [Anthropic 消息流](https://platform.claude.com/docs/en/build-with-claude/streaming)各自定义结束边界。分开记录首事件和流内空闲，心跳不能重置空闲计时；不记录原始推理。保留供应商默认推理强度，先用真实的阶段耗时区分等待与长回复，不凭历史笼统错误调高全局超时或削减推理。
- 已知临时故障继续用原模型组和原 Run 退避；对没有具体错误码的安全模型断点最多多给一次同 Run 的机会，不因笼统错误跨接入切换。不把额度、权限或未知工具效果当网络故障。已挂起的旧任务仍由控制台「继续原任务」，升级不自动处置历史阻塞记录。
- 直接用持久记录答当前进度和最近产出；区分这轮受阻、上一轮历史失败、工具已执行、结果已验收。不从聊天承诺推断执行或从源码猜一条命令自动跑。

## 后果

现有数据可直接读；旧文本模型、替身及离线运行保留兼容。原生模式的可用性以网关协议为准，允许逐条接入退回文本；没有通过工具完成标记的半截参数只作失败处理。若要回退到不认识 `execution_mode` 的旧二进制，先在新版将显式模式改为「自动」（不写入此字段）。未来如需并行工具、供应商原生长历史或分支推理，另做设计。详见 [设计与验收](../DESIGN.md#model-decision-transport)。
