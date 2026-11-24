# 编码循环可见性与 iOS 交付可达性（M82）

> **设计与验收记录。** 当前完整方案统一阅读 [PROTO 总设计](../DESIGN.md)；本文件保留 M82 的证据、取舍与验收清单。

## 1. 背景：一次独立核查的结论

主人指令：让 iPhone 专家完全自主地「提出想法 → 设计 → 开发 → 本地验证 → 交付成熟完整的 App」。2026-11-24 14:30 的只读核查表明目标未达成，且 M80「监督者可离场」与 M81「三缺口闭合」的结论在部署后两小时内再次被证伪：

- 生产重启（10:05）后专家 10:13→12:06 消耗约 140 万 token、344 次模型调用，读文件 200 次、跑命令 102 次，**写文件只有 10 次**；13 条任务全部失败，随后因零进展阻塞、自动恢复配额耗尽而空等。
- 活动工程 `Sources/Wengu` 只有 3 个文件 239 行；在副本上真实 `swift build` 失败。四个文件各写了一套互不兼容的 SM-2 接口。
- 工作区没有任何 `.xcodeproj`；验收条款与交付门要求的模拟器 `.app` 回执从未产生。

## 2. 根因（逐条有生产证据）

1. **模型看不到修错误所需的信息。** `work_context.decision_input` 在累计观察超过 12 KiB 后进入压缩模式：只保留最近 2 条观察（各截 1400 字节），超过 900 字节的读文件结果被替换成「大段正文已折叠」。实测 24 步的输入估算始终 3.8k–4.5k，完全不随步骤增长。于是「跑构建能看到报错，但下一步读文件时正文不可见」→ 写不出修复 → 只能凭想象整文件重写 → 再读再构建直到步数耗尽。
2. **命令结果的有用部分恰好最先被裁掉。** `run_command` 的观察是 Python 字典的字符串形式：`stdout` 在前、`stderr` 在后，而 `swift build` 的 `stderr` 以一条无关的 SwiftPM 缓存警告开头。`_clip` 保留头尾，但压缩再截前 1400 字节——剩下的是构建横幅和那条警告，真正的编译错误全部丢失。
3. **「工位现状」误导。** `CodeTools.list_dir` 以工作区根计算深度，对 `projects/<工位>` 调用时深度已是 2，子目录全部被剪掉；`agentic._prepare_prompt_blocks` 又只截前 400 字符。模型看到的「工作区当前内容」是 3 个顶层文件，一个 Swift 源码都没有，因此反复「确认源码是否真实存在」。
4. **重复验证不被拦截。** `fail_repeat` 的判重键取输出前 120 字符（构建横幅），分不清错误有没有变化；同一构建命令在 23 步里跑了 7 次，每次都真实执行。
5. **验收在结构上不可达。** 交付门要求 `xcodebuild -project X.xcodeproj …` 产出 `*-iphonesimulator/*.app`，但系统没有任何办法生成 `.xcodeproj`（测试里用的是手工夹具 `tests/fixtures/ios`）。本机也没有验收文字里写的 iPhone 16 模拟器。
6. **沙盒挡住 xcodebuild。** 宏插件会再套一层 sandbox-exec（M80 只修了 `swift`/`swiftc`，没覆盖 xcodebuild）；测试通道还要 launchd 套接字目录、unix socket 与伪终端。

## 3. 目标、非目标与反目标

**目标**

1. 修编译错误时，模型在同一步里能看到错误诊断和要改的文件正文。
2. 重复的验证命令不再真实执行；判重看诊断而不是横幅。
3. 工位文件清单与接口轮廓可信，跨文件引用不再靠想象。
4. iOS 应用工程可由专家自己生成，并在沙盒内完成模拟器构建与 XCTest。
5. 以上每一项都有回归测试，并在真实模型的 Wengu 副本上 A/B 验证。

**非目标**

- 不更换执行模型，不改用外部编码 CLI 代写（是否采用 `delegate_coding` 由主人决定）。
- 不放宽 owner-only、不可逆与可见写审批；不开放 TCP/IP 网络。
- 不降低交付门：模拟器 `.app` 与真实测试回执仍是硬条件。

**反目标（做到这些就算失败）**

- 靠无限调大上下文换取「能看见」——成本随步数平方增长，工程变大后照样崩。
- 只改提示词不改机制——生产已证明软提示会被无视。
- 单元测试全绿但没有真实模型跑通。

## 4. 方案比较

| 方案 | 做法 | 取舍 | 结论 |
|---|---|---|---|
| A 只调大预算 | 把历史上限从 4 KiB 调到 64 KiB | 改动最小；小工程有效；但每步重发整段历史，成本随步数平方增长，工程变大后仍要压缩，压缩规则不修就回到原点 | 不单独采用；预算适度上调作为组合的一部分 |
| B 按需保留（选定） | 压缩时按信息类别保留：错误诊断优先、最近读过的文件保留正文、旧读取折叠成指针；命令观察重排为「退出码 → 错误摘要 → 输出尾部」 | 每步都能看见「报错 + 文件」，成本有界且与工程大小无关；实现集中在两个纯函数模块，可确定性测试 | **采用** |
| C 更换执行模型 / 委派外部编码 CLI | 专家只做设计，实现交给成熟编码代理 | 最可能一次做出完整 App；但改变产品定位，且需要主人在场授权 | 暂缓，已向主人说明 |

## 5. 设计

### 5.1 命令观察整形（`proto/exec/observations.py`，新模块，纯函数）

`run_command` / `run_python` 的观察文本改为确定性的三段式，重要的在前：

```
退出码 1（nonzero_exit）
─ 错误摘要 3 条 ─
Sources/Wengu/Models.swift:7:6: error: 'Unique' is only available in iOS 18 or newer
   7 |     #Unique<Deck>([\.name])
Sources/Wengu/Models.swift:30:16: error: A default value requires a fully qualified domain named value
  30 |     public var dueDate: Date = .now
─ 输出尾部 ─
…
```

- 诊断识别：clang/Swift 的 `路径:行:列: error:`（附带其后最多 3 行源码摘录）、xcodebuild 的「The following build commands failed」清单、Python 的 `File "…", line N` 与最终异常行、pytest 的 `FAILED`/`ERROR`/`E `、通用的 `error TS`、`npm ERR!`、`Error:`、`not permitted`。
- 绝对路径裁成工位相对路径，去掉 ANSI 颜色码，去重，最多 12 条 / 2400 字节。
- 成功的命令保留头尾（`-list`、`simctl list` 这类输出头部同样重要）。
- 原始字典仍完整落进会话事件与执行日志；整形只影响模型与时间线看到的文本。

### 5.2 压缩策略（`proto/exec/work_context.py`）

| 项 | 现值 | 新值 |
|---|---|---|
| 输入总预算默认（`PROTO_ACTOR_INPUT_TOKENS`） | 32768 | 65536（硬顶仍 128 KiB） |
| 未压缩历史上限 | 12 KiB | 24 KiB |
| 压缩后历史预算 | 4 KiB | 12 KiB |
| 紧急模式历史预算 | 1.2 KiB | 3 KiB |

压缩时从最新观察向前走，按类别保留，直到预算用尽（输出仍按时间顺序）：

- **命令结果**：最多 3000 字节（错误摘要在前，所以裁尾不丢诊断）。
- **文件读取**：最近 2 个不同文件保留正文，各最多 6000 字节；更早的读取折叠成指针，并写明怎么取回（「改它之前的那一步用 `read_file(path, offset, limit≤80)` 重新读」）。
- **过期失效**：某个路径被 `write_file`/`edit_file` 写过之后，它更早的读取标记为「已修改，内容过期」，避免模型按旧内容构造 `old` 片段。
- **系统提示**（「提示：」「拦截：」「参数错误」等）：原样保留。
- **其余**（写入回执、grep、符号检索）：最多 800 字节。

紧急模式（供应商明确回报上下文超限后的一次性回退）保持现有行为：折叠全部文件正文、丢弃辅助信息。

### 5.3 重复命令短路（`proto/exec/agentic.py`）

执行 `run_command`/`run_python` 之前，在本会话轨迹里找同工具、同参数的上一条记录；若此后**只有只读工具**执行过，就不真实执行，而是重放那条记录的结果并说明「此后没有文件修改，重跑不会改变结果」。有任何非只读步骤（写入、其他命令）则照常执行。交付阶段的 iOS 构建取证命令不参与短路（它要求全新的 `-derivedDataPath`）。原有的「相同参数连续第 3 次拦截」保留。

### 5.4 判重与工位清单

- `fail_repeat` 的键改为错误摘要的规范化文本；没有诊断时退回输出开头。
- `CodeTools.list_dir` 的深度改为相对被列目录计算，最多 3 层，跳过 `.build`、`.proto-build*`、`DerivedData`；观察上限提高到 2000。
- 「工位文件」提示块给 1400 字符并注明是开工时的快照。

### 5.5 接口轮廓（`proto/exec/outline.py`，新模块）

每步固定区附带工位源码的**类型与成员签名**（Swift 的 struct/class/enum/protocol/actor 及其属性、方法、init；Python 用 AST；JS/TS 用声明正则），总预算 2400 字节，按（路径、mtime、大小）指纹缓存，源码变化才重算。目的是让跨文件引用有事实依据：`Card` 有的是 `easeFactor` 而不是 `easiness`。

### 5.6 iOS 工程生成与沙盒可达

- **新技能 `ios-app-project`**：附带 `make_xcodeproj.py`（仅标准库）。专家写一份 JSON 清单（应用名、bundle id、源码目录、资源、可选单元测试 target），运行脚本得到确定性的 `.xcodeproj` 与共享 scheme。对象标识由名称派生，重复生成不改文件内容。
- **xcodebuild 旗标注入**：`sandbox-exec` 后端下，`run_command` 对 `xcodebuild` 的构建/测试动作自动追加 `OTHER_SWIFT_FLAGS=-Xfrontend -disable-sandbox`，与 M80 对 `swift`/`swiftc` 的处理同理。
- **沙盒放行**（均为本机 IPC 原语，不涉及 TCP/IP，不扩大工作区外的持久写入）：
  - 当前用户的 launchd 会话套接字目录 `/private/var/tmp/com.apple.launchd.*/`（testmanagerd 通道）；
  - `network-outbound` 到 unix socket；
  - 伪终端分配 `/dev/ptmx` 与 `/dev/ttysN`。
- **模拟器就绪**：`xcrun simctl boot` 与 `bootstatus -b` 可在沙盒内执行；冷启动后的首次测试需要等模拟器稳定（具体等待规则由实验确定，见 §7）。
- `inspect_build` 在「只有 `Package.swift`、没有 Xcode 工程」时提示先读 `ios-app-project` 技能；交付阶段的任务提示补上同一句，并说明模拟器名称以 `simctl list devices available` 为准。

### 5.7 阻塞通知

交付阻塞的自动恢复配额耗尽时，向收件箱写一条「需要你」的消息，每个阻塞周期只写一次；主人手动重试时清除标记。专家停下来等人时主人不再需要靠刷新活动中心才知道。

## 6. 验收清单

1. 压缩模式下，构建失败观察里的全部错误摘要行对模型可见；构建横幅与 SwiftPM 警告不占据摘要。
2. 压缩模式下最近 2 个文件的读取正文可见；路径被写入后，更早的读取带「已过期」标记；紧急模式仍折叠全部正文。
3. 预算上表四个数字生效；固定区超预算失败关闭、供应商上下文超限紧缩重试一次的既有行为不变；`test_m71_continuity`、`test_m77_scoped_exploration` 的既有断言继续通过或按新契约显式迁移。
4. 只读步之间重复同一验证命令不真实执行，观察说明原因并带回原结果；中间有写入或其它命令则真实执行。
5. `fail_repeat` 的键随诊断变化而变化。
6. `list_dir(工位)` 能列出 `Sources/**/*.swift`；工位文件块包含源码文件。
7. 接口轮廓随源码修改刷新，且不超过 2400 字节。
8. 生成的 `.xcodeproj` 能被 `xcodebuild -list` 识别；沙盒内 `xcodebuild … build` 退出 0 并产出 `.app`；沙盒内 `xcodebuild … test` 在已就绪的模拟器上通过。
9. 沙盒仍然拒绝工作区外的写入与非 unix-socket 的网络访问。
10. 交付阻塞配额耗尽后收件箱恰有一条通知；主人重试后标记清除。
11. 真实模型 A/B：同一份 Wengu 三文件副本，改造前一个工作片零写入；改造后 `swift build` 退出 0。

## 7. 实验记录

- 生成器产出的工程在沙盒外 `xcodebuild build` 通过；沙盒内补上 §5.6 的旗标与放行后构建通过（12 秒）并产出 `Debug-iphonesimulator/*.app`。
- 沙盒内 `xcodebuild test` 在已运行一段时间的模拟器上通过（27 秒，`Executed 1 test, with 0 failures`）。
- 沙盒内外，**冷启动后的首次** `xcodebuild test` 均挂起超过 280 秒：探针显示 SpringBoard 在启动后约 130 秒消失（模拟器重启）。等待规则待补。
