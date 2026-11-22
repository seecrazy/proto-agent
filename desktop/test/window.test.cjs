"use strict";

const assert = require("node:assert/strict");
const {createHmac} = require("node:crypto");
const {test} = require("node:test");
const {CHALLENGE, PROOF, origin} = require("../backend.cjs");
const {protectSession} = require("../window.cjs");

const endpoint = {port: 8431, key: "22".repeat(32), token: "private-test-owner-token"};

test("a window receives API auth only after a signed local main frame and refuses redirects or frames", () => {
  const listeners = {};
  const set = name => (_filter, handler) => { listeners[name] = handler; };
  const session = {webRequest: {onBeforeRequest: set("request"), onBeforeSendHeaders: set("send"),
    onHeadersReceived: set("receive"), onErrorOccurred: set("error")},
  setPermissionRequestHandler: handler => handler(null, "media", allowed => assert.equal(allowed, false)),
  setPermissionCheckHandler: handler => assert.equal(handler(), false),
  setDevicePermissionHandler: handler => assert.equal(handler(), false), on() {}, removeListener() {}};
  protectSession(session, endpoint);
  const main = {id: 1, url: `${origin(endpoint)}/`, method: "GET", resourceType: "mainFrame"};
  const api = {id: 2, url: `${origin(endpoint)}/api/agents`, method: "GET", resourceType: "xhr",
    requestHeaders: {"x-proto-token": "placeholder", Accept: "application/json"}};
  const verify = (name, details, expected) => listeners[name](details, actual => assert.deepEqual(actual, expected));
  verify("request", api, {cancel: true});
  verify("send", api, {requestHeaders: {Accept: "application/json"}});
  verify("request", main, {cancel: false});
  const proofFor = navigation => {
    let signature;
    listeners.send(navigation, result => {
      assert.match(result.requestHeaders[CHALLENGE], /^[0-9a-f]{64}$/);
      assert(!Object.values(result.requestHeaders).includes(endpoint.token));
      signature = createHmac("sha256", Buffer.from(endpoint.key, "hex"))
        .update(result.requestHeaders[CHALLENGE]).digest("hex");
    });
    return signature;
  };
  const proof = proofFor(main);
  verify("receive", {...main, statusCode: 200, responseHeaders: {[PROOF]: [proof]}}, {cancel: false});
  verify("request", api, {cancel: false});
  verify("send", api, {requestHeaders: {Accept: "application/json", "X-Proto-Token": endpoint.token}});
  for (const url of ["https://example.test/", "http://localhost:8431/", "http://127.0.0.1:8000/",
    "file:///tmp/example", `${origin(endpoint)}/api/agents`]) {
    verify("request", {...main, url}, {cancel: true});
  }
  verify("request", {...main, resourceType: "subFrame"}, {cancel: true});
  verify("request", {...api, url: "https://example.test/api/agents"}, {cancel: true});
  verify("send", {...api, url: "https://example.test/api/agents"}, {requestHeaders: {Accept: "application/json"}});
  const reload = {...main, id: 3};
  const secondProof = proofFor(reload);
  assert.notEqual(secondProof, proof);
  verify("receive", {...reload, statusCode: 302, responseHeaders: {[PROOF]: [secondProof]}}, {cancel: true});
  verify("request", api, {cancel: true});
  const third = {...main, id: 4};
  proofFor(third);
  verify("receive", {...third, statusCode: 200, responseHeaders: {[PROOF]: [proof]}}, {cancel: true});
});
