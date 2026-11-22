"use strict";

const assert = require("node:assert/strict");
const {createHmac} = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const {test} = require("node:test");
const {DesktopBackend, parseOptions, readEndpoint, checkEndpoint, CHALLENGE, PROOF} = require("../backend.cjs");

const TOKEN = "fixture-token-not-for-renderer";
const KEY = "11".repeat(32);

function scratch(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "proto-desktop-node-"));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  return directory;
}

test("startup resolves the requested fleet from the project root and validates port flags", t => {
  const root = scratch(t);
  assert.deepEqual(parseOptions(["--data-dir", "team", "--port", "0", "--connect-only"],
    {root, env: {PROTO_PYTHON: "custom-python"}}), {root, dataDir: path.join(root, "team"),
    port: 0, connectOnly: true, python: "custom-python", env: {PROTO_PYTHON: "custom-python"}});
  assert.equal(parseOptions([], {root, env: {}}).port, 8431);
  for (const args of [["--port", "65536"], ["--port", "https://example.test"], ["--data-dir"], ["--url"]]) {
    assert.throws(() => parseOptions(args, {root, env: {}}));
  }
});

test("readEndpoint accepts only a private same-user regular descriptor with supported fields", t => {
  const root = scratch(t);
  const privateDir = path.join(root, ".proto-console");
  const record = path.join(privateDir, "endpoint.json");
  fs.mkdirSync(privateDir, {mode: 0o700});
  const expected = {version: 1, data_dir: fs.realpathSync(root), pid: 1234, port: 8431, key: KEY, token: TOKEN};
  const write = fields => fs.writeFileSync(record, JSON.stringify(fields), {mode: 0o600});
  assert.equal(readEndpoint(root), null);
  write(expected);
  assert.deepEqual(readEndpoint(root), expected);
  fs.chmodSync(privateDir, 0o755);
  assert.equal(readEndpoint(root), null);
  fs.chmodSync(privateDir, 0o700);
  fs.chmodSync(record, 0o644);
  assert.equal(readEndpoint(root), null);
  fs.chmodSync(record, 0o600);
  for (const patch of [{version: 2}, {data_dir: "/other-fleet"}, {port: 0}, {port: "8431"}, {pid: -1},
    {key: "invalid"}, {token: "line\nbreak"}]) {
    write({...expected, ...patch});
    assert.equal(readEndpoint(root), null);
  }
  fs.renameSync(record, path.join(privateDir, "other.json"));
  fs.symlinkSync("other.json", record);
  assert.equal(readEndpoint(root), null);
});

test("discovery sends no owner credential and refuses another 200 page or replayed proof", async t => {
  const root = scratch(t);
  let reply = "correct";
  const seen = [];
  let firstProof;
  const server = http.createServer((request, response) => {
    seen.push(request.headers);
    const signature = createHmac("sha256", Buffer.from(KEY, "hex"))
      .update(request.headers[CHALLENGE.toLowerCase()]).digest("hex");
    firstProof ??= signature;
    if (reply !== "unrelated") response.setHeader(PROOF, reply === "replay" ? firstProof : signature);
    response.end("local fixture");
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());
  const endpoint = {port: server.address().port, pid: process.pid, version: 1, data_dir: fs.realpathSync(root),
    key: KEY, token: TOKEN};
  assert.equal(await checkEndpoint(endpoint), true);
  reply = "replay";
  assert.equal(await checkEndpoint(endpoint), false);
  reply = "unrelated";
  assert.equal(await checkEndpoint(endpoint), false);
  assert(seen.every(headers => !Object.values(headers).includes(TOKEN)));
  assert.equal(new Set(seen.map(headers => headers[CHALLENGE.toLowerCase()])).size, 3);
  fs.mkdirSync(path.join(root, ".proto-console"), {mode: 0o700});
  fs.writeFileSync(path.join(root, ".proto-console", "endpoint.json"), JSON.stringify(endpoint), {mode: 0o600});
  const backend = new DesktopBackend(parseOptions(["--connect-only", "--data-dir", root]));
  await assert.rejects(backend.connect(), /未找到/);
  assert.equal(backend.child, null);
  reply = "correct";
  assert.deepEqual(await backend.connect(), endpoint);
  backend.close();
  assert.equal(await checkEndpoint(endpoint), true);
});
