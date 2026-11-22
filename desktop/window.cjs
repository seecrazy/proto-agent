"use strict";

const {challenge, origin, verifyProof, CHALLENGE, PROOF} = require("./backend.cjs");

function localURL(raw, serviceOrigin) {
  try {
    const url = new URL(raw);
    return url.protocol === "http:" && url.origin === serviceOrigin && !url.username && !url.password ? url : null;
  } catch {
    return null;
  }
}

function isConsolePage(url) {
  return url && (url.pathname === "/" || url.pathname === "/index.html");
}

function blockDownload(event) { event.preventDefault(); }

function protectSession(session, endpoint) {
  const serviceOrigin = origin(endpoint);
  const filter = {urls: ["<all_urls>"]};
  const pending = new Map();
  let verified = false;
  session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.setPermissionCheckHandler(() => false);
  session.setDevicePermissionHandler(() => false);
  session.removeListener("will-download", blockDownload);
  session.on("will-download", blockDownload);
  session.webRequest.onBeforeRequest(filter, (details, callback) => {
    const url = localURL(details.url, serviceOrigin);
    if (details.resourceType === "mainFrame") {
      callback({cancel: details.method !== "GET" || !isConsolePage(url)});
    } else {
      const image = details.resourceType === "image" && details.url.startsWith("data:image/");
      callback({cancel: details.resourceType === "subFrame" || (!image && !(verified && url))});
    }
  });
  session.webRequest.onBeforeSendHeaders(filter, (details, callback) => {
    const headers = {...details.requestHeaders};
    for (const key of Object.keys(headers)) {
      if (["x-proto-token", CHALLENGE.toLowerCase()].includes(key.toLowerCase())) delete headers[key];
    }
    const url = localURL(details.url, serviceOrigin);
    if (details.resourceType === "mainFrame" && isConsolePage(url)) {
      verified = false;
      const nonce = challenge();
      pending.set(details.id, nonce);
      headers[CHALLENGE] = nonce;
    } else if (verified && url?.pathname.startsWith("/api/")) {
      headers["X-Proto-Token"] = endpoint.token;
    }
    callback({requestHeaders: headers});
  });
  session.webRequest.onHeadersReceived(filter, (details, callback) => {
    if (details.resourceType !== "mainFrame") return callback({});
    const nonce = pending.get(details.id);
    pending.delete(details.id);
    const proof = Object.entries(details.responseHeaders || {}).find(([name]) => name.toLowerCase() === PROOF)?.[1];
    verified = Boolean(nonce && details.statusCode === 200 && proof?.length === 1
      && verifyProof(endpoint, nonce, proof[0]));
    callback({cancel: !verified});
  });
  session.webRequest.onErrorOccurred(filter, details => pending.delete(details.id));
}

async function createConsoleWindow(electron, endpoint, {show = true} = {}) {
  const session = electron.session.fromPartition("persist:proto-console");
  await session.setProxy({mode: "direct"});
  protectSession(session, endpoint);
  const window = new electron.BrowserWindow({width: 1440, height: 920, minWidth: 400, minHeight: 520,
    title: "PROTO 控制台", backgroundColor: "#10131a", show: false,
    webPreferences: {session, nodeIntegration: false, contextIsolation: true, sandbox: true,
      webSecurity: true, webviewTag: false, navigateOnDragDrop: false, devTools: false}});
  const serviceOrigin = origin(endpoint);
  window.webContents.setWindowOpenHandler(() => ({action: "deny"}));
  window.webContents.on("will-attach-webview", event => event.preventDefault());
  window.webContents.on("will-redirect", event => event.preventDefault());
  window.webContents.on("will-navigate", (event, target) => {
    if (!isConsolePage(localURL(event.url || target, serviceOrigin))) event.preventDefault();
  });
  try {
    await window.loadURL(`${serviceOrigin}/#token=${challenge()}`);
    if (show) window.show();
    return window;
  } catch (error) {
    window.destroy();
    throw error;
  }
}

module.exports = {createConsoleWindow, protectSession};
