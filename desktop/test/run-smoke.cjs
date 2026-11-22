"use strict";

const {spawnSync} = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const executable = require("electron");

const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "proto-electron-smoke-"));
try {
  const result = spawnSync(executable, [path.join(__dirname, "smoke.cjs"), scratch], {stdio: "inherit"});
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
} finally {
  fs.rmSync(scratch, {recursive: true, force: true});
}
