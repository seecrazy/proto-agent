"use strict";

const {spawn} = require("node:child_process");
const {createHmac, randomBytes, timingSafeEqual} = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const {setTimeout: wait} = require("node:timers/promises");

const ROOT = path.resolve(__dirname, "..");
const DIRECTORY = ".proto-console";
const CHALLENGE = "X-Proto-Desktop-Challenge";
const PROOF = "x-proto-desktop-proof";
const HEX_KEY = /^[0-9a-f]{64}$/;
const HEADER_TOKEN = /^[\x21-\x7e]{1,1024}$/;

class LaunchError extends Error {}

function parseOptions(args, {root = ROOT, env = process.env} = {}) {
  let dataDir = path.join(root, "proto_data"), port = 8431, connectOnly = false;
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (flag === "--connect-only") {
      connectOnly = true;
    } else if (flag === "--data-dir" || flag === "--port") {
      const value = args[++index];
      if (!value || value.startsWith("--")) throw new LaunchError(`${flag} 缺少参数。`);
      if (flag === "--data-dir") {
        dataDir = path.resolve(root, value);
      } else {
        if (!/^\d+$/.test(value) || Number(value) > 65535) throw new LaunchError("端口应为 0 至 65535 的整数。");
        port = Number(value);
      }
    } else {
      throw new LaunchError("支持 --data-dir 路径、--port 端口及 --connect-only。");
    }
  }
  if (fs.existsSync(dataDir)) dataDir = fs.realpathSync(dataDir);
  const venvPython = path.join(root, ".venv", "bin", "python");
  const python = env.PROTO_PYTHON || (fs.existsSync(venvPython) ? venvPython : "python3");
  return {root, dataDir, port, connectOnly, python, env};
}

function isPrivate(info) {
  return info.uid === process.getuid() && (info.mode & 0o077) === 0;
}

function readEndpoint(dataDir) {
  if (!process.getuid) return null;
  const directory = path.join(dataDir, DIRECTORY);
  let fd;
  try {
    const parent = fs.lstatSync(directory);
    if (!parent.isDirectory() || !isPrivate(parent)) return null;
    fd = fs.openSync(path.join(directory, "endpoint.json"), fs.constants.O_RDONLY
      | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    const info = fs.fstatSync(fd);
    if (!info.isFile() || !isPrivate(info) || info.size > 4096) return null;
    const endpoint = JSON.parse(fs.readFileSync(fd, "utf8"));
    if (endpoint?.version !== 1 || endpoint.data_dir !== fs.realpathSync(dataDir)
        || !Number.isSafeInteger(endpoint.pid) || endpoint.pid < 1
        || !Number.isInteger(endpoint.port) || endpoint.port < 1 || endpoint.port > 65535
        || typeof endpoint.key !== "string" || !HEX_KEY.test(endpoint.key)
        || typeof endpoint.token !== "string" || !HEADER_TOKEN.test(endpoint.token)) return null;
    return endpoint;
  } catch {
    return null;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function origin(endpoint) {
  return `http://127.0.0.1:${endpoint.port}`;
}

function challenge() {
  return randomBytes(32).toString("hex");
}

function verifyProof(endpoint, nonce, proof) {
  if (typeof proof !== "string" || !HEX_KEY.test(proof)) return false;
  const expected = createHmac("sha256", Buffer.from(endpoint.key, "hex")).update(nonce).digest();
  return timingSafeEqual(expected, Buffer.from(proof, "hex"));
}

function checkEndpoint(endpoint, timeoutMs = 1000) {
  const nonce = challenge();
  return new Promise(resolve => {
    const request = http.get(`${origin(endpoint)}/`, {agent: false, headers: {[CHALLENGE]: nonce}}, response => {
      response.on("error", () => {});
      response.resume();
      resolve(response.statusCode === 200 && verifyProof(endpoint, nonce, response.headers[PROOF]));
    });
    const timer = setTimeout(() => request.destroy(), timeoutMs);
    request.on("close", () => { clearTimeout(timer); resolve(false); });
    request.on("error", () => resolve(false));
  });
}

class DesktopBackend {
  constructor(options, {waitMs = 12000} = {}) {
    this.options = options;
    this.waitMs = waitMs;
    this.child = null;
    this.failure = false;
    this.closed = false;
  }

  async discover() {
    const endpoint = readEndpoint(this.options.dataDir);
    return endpoint && await checkEndpoint(endpoint) ? endpoint : null;
  }

  async connect() {
    const existing = await this.discover();
    if (existing) return existing;
    if (this.closed) throw new LaunchError("控制台已退出。");
    const {root, dataDir, port, connectOnly, python, env} = this.options;
    if (connectOnly) throw new LaunchError("未找到该数据目录的新版 Web 服务。请先正常启动或重启 Web，再重连。");
    if (!this.child || this.failure || this.child.exitCode !== null || this.child.signalCode) {
      this.failure = false;
      this.child = spawn(python, ["-u", "-m", "proto.web", "--data-dir", dataDir,
        "--port", String(port), "--no-open", "--data-env-only"], {cwd: root, env, stdio: "ignore"});
      this.child.once("error", () => { this.failure = true; });
      this.child.unref();
    }
    const deadline = Date.now() + this.waitMs;
    while (!this.closed && Date.now() < deadline) {
      const endpoint = await this.discover();
      if (endpoint) return endpoint;
      if (this.failure) throw new LaunchError("无法启动 Python。请安装 Python 3.9+，或用 PROTO_PYTHON 指定解释器。");
      if (this.child.exitCode !== null || this.child.signalCode) {
        throw new LaunchError("本机 Web 启动失败。请检查数据目录是否已有旧服务、端口是否占用，或在终端启动 Web 查看错误。");
      }
      await wait(150);
    }
    throw new LaunchError("未收到可验证的本机服务响应。请检查 Web 启动状态，或稍后重连。");
  }

  close() {
    this.closed = true;
    if (this.child && this.child.exitCode === null && !this.child.signalCode && !this.child.killed) {
      this.child.kill("SIGINT");
    }
  }
}

module.exports = {DesktopBackend, LaunchError, parseOptions, readEndpoint, checkEndpoint, origin,
  challenge, verifyProof, CHALLENGE, PROOF};
