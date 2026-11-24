---
name: ios-app-project
description: 用一份 JSON 清单生成可被 xcodebuild 构建与测试的 iOS .xcodeproj（应用 + 可选单元测试 + 可选 UI 测试）
---

# 生成 iOS 应用工程

交付 iOS 应用要有 `.xcodeproj`：模拟器构建与 XCTest 回执都以它为入口；只有 `Package.swift` 时 `swift build` 通过不等于得到 iPhone App。

1. 在工位写清单 `project.json`：
   `{"name":"Wengu","bundle_id":"com.example.wengu","deployment_target":"17.0","sources":["Sources/Wengu"],"resources":["Resources/Assets.xcassets"],"tests":{"name":"WenguTests","bundle_id":"com.example.wengu.tests","sources":["Tests/WenguTests"]},"ui_tests":{"name":"WenguUITests","bundle_id":"com.example.wengu.uitests","sources":["UITests/WenguUITests"]}}`
   `name`/`tests.name`/`ui_tests.name` 只用字母数字下划线且互不相同；三个 bundle_id 互不相同。`sources` 填目录（递归收 .swift/.m/.c）或文件；`resources`、`tests`、`ui_tests` 可省略。可选 `info`（键值对，变成 INFOPLIST_KEY_*，如 `"UISupportedInterfaceOrientations":["UIInterfaceOrientationPortrait"]`）、`settings`（构建设置，可按 `"Debug"`/`"Release"` 分组）、`device_family`（`"1"` 仅 iPhone）。
2. `run_command`：`python3 <scripts.make_xcodeproj 的绝对路径> project.json`，cwd 为工位。脚本只写 `<name>.xcodeproj`，重复生成不改内容；源码增删后重新生成即可。清单有错会以退出码 2 指出字段。
3. 接着按 swift-ios-build 技能：`xcodebuild -project <name>.xcodeproj -list` 确认 scheme，再构建、测试。单元测试文件用 `@testable import <name>`。
4. **要在模拟器里走 UI 主流程（点按、翻卡、滑动），必须用 `ui_tests`**（XCUITest）：代码里 `let app = XCUIApplication(); app.launch()`，用 `app.buttons["…"].tap()` 等断言流程，`xcodebuild test` 会在模拟器里装 App、启动并执行点按——这不需要人手点屏幕，也不要把 simctl install/launch 当成需要主人代劳的事。单元测试 target 不能做 UI 点按（它注入 App 进程，没有 XCUIApplication 运行时）。

脚本路径见本结果里的 `scripts.make_xcodeproj`。
