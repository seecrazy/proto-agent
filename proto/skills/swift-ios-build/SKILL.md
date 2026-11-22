---
name: swift-ios-build
description: Swift/iOS 的工程发现、Xcode 模拟器构建及 XCTest 排障
---

# 在当前工位验证 Swift 或 iOS

1. 用 `inspect_build` 确认运行后端和工程入口。PATH 中找到命令只表示可尝试；用 `run_command` 的 `xcodebuild -version`、`xcodebuild -showsdks` 找实际 Xcode 和 Simulator SDK。`Package.swift` 可用 `swift build` / `swift test`，但通过不等于得到 iPhone App。
2. 对明确的工程/工作区，用 `xcodebuild -project App.xcodeproj -list -disableAutomaticPackageResolution`（若项目使用 workspace 则用 `-workspace App.xcworkspace`）查真实 scheme。传数组 argv，cwd 必须是实际工位，不猜 target、SDK 或命令不可用。
3. 用 `xcodebuild -project App.xcodeproj -scheme App -destination "generic/platform=iOS Simulator" -derivedDataPath .proto-build-01 -disableAutomaticPackageResolution CODE_SIGNING_ALLOWED=NO build`。把示例替换为发现的入口/scheme、一个不存在的 `.proto-build-*` 路径。退出 0 和本次生成的 `*-iphonesimulator/*.app` 是构建证据；不需要签名或 App Store 账号。
4. 需要 XCTest 时，用 `xcrun simctl list devices available` 查本地设备，选择实际存在的具体目的地，再跑 `xcodebuild … test`。`build-for-testing` 或 0 个测试不算通过。SDK 不等于已安装可用于测试的 runtime。
5. 遇到失败，区分：执行器/命令找不到、xcode-select/SDK、入口/scheme、依赖、源码/链接、模拟器 runtime/测试。保留命令与错误，先修本工位可修问题；不切 Xcode、不装依赖、不绕开沙盒。
