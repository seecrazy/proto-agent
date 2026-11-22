"use strict";

const assert = require("node:assert/strict");
const {once} = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const {setTimeout: wait} = require("node:timers/promises");
const electron = require("electron");
const {app} = electron;
const {DesktopBackend, parseOptions, readEndpoint, origin} = require("../backend.cjs");
const {createConsoleWindow} = require("../window.cjs");

const scratch = process.argv[2];
const profile = path.join(scratch, "profile");
fs.mkdirSync(profile);
app.setPath("userData", profile);
app.commandLine.appendSwitch("disable-background-networking");
app.on("window-all-closed", () => {});

async function retry(read) {
  const deadline = Date.now() + 6000;
  do {
    const value = await read();
    if (value) return value;
    await wait(75);
  } while (Date.now() < deadline);
  throw new Error("Electron did not render the expected console state");
}

async function smoke() {
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("PROTO_") || key === "PROTO_PYTHON"));
  const dataDir = path.join(scratch, "fleet");
  const args = ["--data-dir", dataDir, "--port", "0"];
  const managed = new DesktopBackend(parseOptions(args, {env}));
  let window;
  try {
    const endpoint = await managed.connect();
    assert.equal(endpoint.pid, managed.child.pid);
    const attached = new DesktopBackend(parseOptions([...args, "--connect-only"], {env}));
    assert.equal((await attached.connect()).pid, endpoint.pid);
    assert.equal(attached.child, null);
    attached.close();

    const web = async (relative, fields) => {
      const response = await fetch(origin(endpoint) + relative, {method: fields ? "POST" : "GET",
        body: fields ? JSON.stringify(fields) : undefined,
        headers: {"X-Proto-Token": endpoint.token, "Content-Type": "application/json"}});
      assert.equal(response.status, 200);
      return response.json();
    };
    const fleet = await web("/api/agents");
    assert(fleet.current);
    window = await createConsoleWindow(electron, endpoint, {show: false});
    const run = script => window.webContents.executeJavaScript(script);
    assert.equal(await retry(() => run('document.getElementById("agent-select").value')), fleet.current);
    assert.equal(await run(`Boolean(document.getElementById("activity-card") && document.getElementById("activity-sync"))`), true);
    assert.equal(await run(`typeof require === "undefined" && typeof process === "undefined"`), true);
    assert.equal((await run("location.hash")).includes(endpoint.token), false);
    assert.equal(await run('window.open("about:blank") === null'), true);

    const renamePath = `/api/agents/${fleet.current}/name`;
    await run(`api("POST", ${JSON.stringify(renamePath)}, {name: "Electron 验证"})`);
    assert.equal((await web("/api/agents")).agents.find(agent => agent.id === fleet.current).name, "Electron 验证");
    await web(renamePath, {name: "Web 验证"});
    const reloaded = once(window.webContents, "did-finish-load", {signal: AbortSignal.timeout(6000)});
    window.reload();
    await reloaded;
    await retry(() => run('document.getElementById("agent-select").textContent.includes("Web 验证")'));
    assert(await managed.discover());
  } finally {
    if (window && !window.isDestroyed()) window.destroy();
    const child = managed.child;
    const exited = child?.pid && child.exitCode === null && !child.signalCode ? once(child, "exit") : null;
    managed.close();
    if (exited) await Promise.race([exited, wait(6000).then(() => { throw new Error("Managed Web did not stop"); })]);
    assert.equal(readEndpoint(dataDir), null);
  }
}

app.whenReady().then(smoke).then(() => {
  console.log("Electron smoke: same Agent and bidirectional updates, no Node or owner token in renderer, clean managed shutdown");
}, error => { console.error(error.message); process.exitCode = 1; }).finally(() => app.exit(process.exitCode || 0));
