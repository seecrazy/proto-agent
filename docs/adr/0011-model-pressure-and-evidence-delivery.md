# ADR 0011：按接入保护模型负载，按本地证据推进交付

- 状态：已采纳（2026-11-21）
- 契约与事件证据：[设计 §15.5](../DESIGN.md#evidence-driven-delivery)

## 取舍

- 应用限速、供应商速率和日预算是三件事。保留 Agent/舰队预算与单 Agent 写者，在模型发送前增设跨角色、跨 Agent 的路由准入；真实 429 持久冷却，拒绝请求不是一次模型用量。用服务来源和凭据标识来对齐已知共享接入，不宣称识别全部账户级额度。未知限额不硬猜固定 RPM，也不以切换密钥规避限制。
- 不把自主等同于反复重新规划。小切片、单一项目、二进制/测试等机械证据先于模型评价；超限则停止该子任务，保留其他可推进工作与人工解除路径。不用文本相似度或任意一次文件写入无限续命；旧工位原地保留，暂停不会重置预算或绕过审批。
- 为本地 App 使用模拟器而非签名工具链：真实应用构建和有效源码绑定，并继续适用质量门。不能只验 `Package.swift`、文件名、Xcode 是否安装或模型报告；也不将模拟器构建当上架、真实设备或完整功能证明。
- 框架验证使用独立样例，不替自主 Agent 操作真实项目或更改其目标。工具链若需要未获授权的访问，应明确报告受限能力，不能在后台退到无隔离命令执行。
- macOS 原子写入依赖 Darwin 用户 TEMP/CACHE；用系统 `getconf` 定位并校验这两个已有例外，不依赖 Python 平台常量，也不为 Xcode 放行主目录。

## 参考

- IETF [RFC 6585 §4](https://www.rfc-editor.org/rfc/rfc6585.html#section-4)：429 与 `Retry-After`。
- 智谱[用户权益](https://docs.bigmodel.cn/cn/guide/platform/equity-explain)：并发权益取决于实际账号和模型。
- Anthropic [Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)：持续会话应用单个可验收增量、持久交接与端到端验证。
- OpenAI [Harness engineering](https://openai.com/index/harness-engineering/) 与 [Run long horizon tasks with Codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex)：明确完成条件、环境与可重复反馈比叠加规划轮数更重要。
