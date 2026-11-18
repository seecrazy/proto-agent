# -*- coding: utf-8 -*-
"""M22: PROTO.app 生成器——macOS 原生壳（WKWebView + swiftc，零 npm/Electron）。

壳进程托管 `python3 -m proto.web` 子进程（退出自动带走），原生窗口加载
本机控制台（令牌经环境变量传递，不落源码）。无 swiftc 时仅产出源码与说明。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SWIFT_SOURCE = r'''
// PROTO.app —— 原生壳：托管 proto.web 服务 + WKWebView 控制台
import Cocoa
import WebKit

let port = ProcessInfo.processInfo.environment["PROTO_PORT"] ?? "PORT_PLACEHOLDER"
let dataDir = "DATA_DIR_PLACEHOLDER"
let configuredToken = Bundle.main.object(forInfoDictionaryKey: "ProtoToken") as? String
let token = ProcessInfo.processInfo.environment["PROTO_WEB_TOKEN"] ?? configuredToken
    ?? UUID().uuidString.replacingOccurrences(of: "-", with: "")

class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var service: Process?

    func applicationDidFinishLaunching(_ n: Notification) {
        // 1) 托管服务进程：python3 -m proto.web --data-dir … --no-open --token …
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["PROTO_PYTHON"]
                                 ?? "PYTHON_PLACEHOLDER")
        proc.arguments = ["-u", "-m", "proto.web", "--data-dir", dataDir,
                          "--port", port, "--no-open", "--token", token]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        try? proc.run()
        self.service = proc

        // 2) 原生窗口
        let rect = NSRect(x: 0, y: 0, width: 1280, height: 860)
        window = NSWindow(contentRect: rect, styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "PROTO Console"
        webView = WKWebView(frame: rect)
        window.contentView = webView
        window.center()
        window.makeKeyAndOrderFront(nil)

        // 3) 等服务就绪后加载控制台（令牌在 URL 片段，不发给服务器）
        let url = "http://127.0.0.1:\(port)/#token=\(token)"
        DispatchQueue.global().async {
            for _ in 0..<60 {
                usleep(500_000)
                if proc.isRunning == false { break }
                if self.isReady(url.replacingOccurrences(of: "#token=\\(token)", with: "")) { break }
            }
            DispatchQueue.main.async { self.webView.load(URLRequest(url: URL(string: url)!)) }
        }
    }

    func isReady(_ url: String) -> Bool {
        guard let u = URL(string: url) else { return false }
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        let task = URLSession.shared.dataTask(with: u) { _, resp, _ in
            ok = (resp as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }
        task.resume()
        _ = sem.wait(timeout: .now() + 1)
        return ok
    }

    func applicationWillTerminate(_ n: Notification) {
        service?.terminate()   // 壳退出 → 服务进程随之结束
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
'''


def build_app(app_dir: Path, data_dir: str, token: str, port: int = 8420,
              compile_swift: bool = True, python_bin: str = None) -> dict:
    import sys
    app_dir = Path(app_dir)
    macos_dir = app_dir / "Contents" / "MacOS"
    res_dir = app_dir / "Contents" / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)

    import json
    info = {
        "CFBundleName": "PROTO", "CFBundleIdentifier": "com.proto.console",
        "CFBundleExecutable": "PROTO", "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": "11.0",
    }
    if token:
        info["ProtoToken"] = token
    (app_dir / "Contents" / "Info.plist").write_text(
        json.dumps(info, indent=2), encoding="utf-8")

    source = (SWIFT_SOURCE
              .replace("PORT_PLACEHOLDER", str(port))
              .replace("DATA_DIR_PLACEHOLDER", str(data_dir))
              .replace("PYTHON_PLACEHOLDER", python_bin or sys.executable))
    swift_path = res_dir / "main.swift"
    swift_path.write_text(source, encoding="utf-8")

    compiled = False
    if compile_swift and shutil.which("swiftc"):
        result = subprocess.run(
            ["swiftc", "-o", str(macos_dir / "PROTO"),
             "-framework", "Cocoa", "-framework", "WebKit", str(swift_path)],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"swiftc 编译失败:\n{result.stderr[-800:]}")
        compiled = True
    return {"path": str(app_dir), "compiled": compiled, "swift": str(swift_path)}
