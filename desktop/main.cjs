"use strict";

const {createHash} = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const electron = require("electron");
const {app, dialog, Menu, shell} = electron;
const {DesktopBackend, LaunchError, parseOptions, origin} = require("./backend.cjs");
const {createConsoleWindow} = require("./window.cjs");

function configure() {
  if (!["darwin", "linux"].includes(process.platform)) throw new LaunchError("当前桌面入口支持 macOS 和 Linux。");
  const options = parseOptions(process.argv.slice(2));
  const profile = createHash("sha256").update(options.dataDir).digest("hex").slice(0, 24);
  const storage = path.join(app.getPath("userData"), profile);
  fs.mkdirSync(storage, {mode: 0o700, recursive: true});
  app.setPath("userData", storage);
  return options;
}

function run(options) {
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }
  const backend = new DesktopBackend(options);
  let window = null, connecting = false, quitting = false;

  async function connect() {
    if (connecting || quitting) return;
    connecting = true;
    try {
      const endpoint = await backend.connect();
      if (quitting) return;
      const previous = window;
      const next = await createConsoleWindow(electron, endpoint);
      window = next;
      next.on("closed", () => { if (window === next) window = null; });
      if (previous && !previous.isDestroyed()) previous.destroy();
    } catch (error) {
      if (quitting) return;
      const detail = error instanceof LaunchError ? error.message : "无法验证或加载本机控制台。请检查服务后重连。";
      const result = await dialog.showMessageBox({type: "error", message: "无法打开 PROTO 控制台", detail,
        buttons: ["重试", "关闭"], defaultId: 0, cancelId: 1});
      if (result.response === 0) setImmediate(connect);
      else if (!window) app.quit();
    } finally {
      connecting = false;
    }
  }

  async function openBrowser() {
    const endpoint = await backend.discover();
    if (endpoint) {
      try {
        await shell.openExternal(`${origin(endpoint)}/#token=${endpoint.token}`);
        return;
      } catch {}
    }
    await dialog.showMessageBox({type: "error", message: "无法在浏览器打开", detail: "请先确认本机服务已连接。"});
  }

  function showWindow() {
    if (window && !window.isDestroyed()) {
      if (window.isMinimized()) window.restore();
      window.focus();
    } else if (app.isReady()) connect();
  }

  app.on("second-instance", showWindow);
  app.on("activate", showWindow);
  app.on("before-quit", () => { quitting = true; backend.close(); });
  app.on("window-all-closed", () => { if (process.platform !== "darwin" && !connecting) app.quit(); });
  app.whenReady().then(() => {
    const items = [
      {label: "连接", submenu: [{label: "重新连接本机服务", accelerator: "CmdOrCtrl+Shift+R", click: connect},
        {label: "在浏览器打开", click: openBrowser}, {type: "separator"}, {role: "quit"}]},
      {role: "editMenu"},
      {label: "视图", submenu: [{role: "reload"}, {role: "resetZoom"}, {role: "zoomIn"}, {role: "zoomOut"}, {role: "togglefullscreen"}]},
    ];
    if (process.platform === "darwin") items.unshift({role: "appMenu"});
    Menu.setApplicationMenu(Menu.buildFromTemplate(items));
    return connect();
  }).catch(() => app.quit());
}

try {
  run(configure());
} catch (error) {
  dialog.showErrorBox("无法启动 PROTO 桌面控制台", error instanceof LaunchError ? error.message : "无法初始化桌面配置。");
  app.exit(1);
}
