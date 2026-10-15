#!/usr/bin/env node
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..');
const sources = [
  { label: 'pages/common/app.js', file: 'pages/common/app.js' },
  { label: 'pages/control/index.html', file: 'pages/control/index.html' },
  { label: 'pages/logs/index.html', file: 'pages/logs/index.html' },
  { label: 'pages/status/index.html', file: 'pages/status/index.html' },
];

function loadScript(source) {
  let code = fs.readFileSync(path.join(repoRoot, source.file), 'utf8');
  if (source.file.endsWith('.html')) {
    const match = code.match(/<script>\s*(\(function[\s\S]*?)\s*<\/script>/);
    assert.ok(match, `embedded core script not found in ${source.file}`);
    code = match[1];
  }

  let now = 0;
  let nextId = 1;
  const timers = new Map();
  function schedule(callback, delay) {
    const id = nextId++;
    timers.set(id, { at: now + delay, callback });
    return id;
  }
  function cancel(id) { timers.delete(id); }
  const clock = {
    tick(ms) {
      const end = now + ms;
      for (;;) {
        const next = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > end) break;
        timers.delete(next[0]);
        now = next[1].at;
        next[1].callback();
      }
      now = end;
    },
    size() { return timers.size; },
  };
  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    setTimeout: schedule,
    clearTimeout: cancel,
  };
  sandbox.window = { setTimeout: schedule, clearTimeout: cancel };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: source.file });
  const K = sandbox.window.KANJYOU;
  K.__testWindow = sandbox.window;
  K.__clock = clock;
  return K;
}

function makeSdk({ ids = ['sub-1', 'sub-2', 'sub-3'], subscribeError, unsubscribeError, unsubscribeAsyncError } = {}) {
  const calls = { subscribe: [], unsubscribe: [], handlers: [] };
  let index = 0;
  const sdk = {
    subscribeSSE(ep, handlers) {
      calls.subscribe.push(ep);
      calls.handlers.push(handlers);
      if (subscribeError) throw subscribeError;
      const id = ids[Math.min(index++, ids.length - 1)];
      return Promise.resolve(id);
    },
    unsubscribeSSE(id) {
      calls.unsubscribe.push(id);
      if (unsubscribeError) throw unsubscribeError;
      if (unsubscribeAsyncError) return Promise.reject(unsubscribeAsyncError);
      return Promise.resolve();
    },
  };
  return { sdk, calls };
}

function makeRejectingSdk() {
  const calls = { subscribe: [], unsubscribe: [] };
  const sdk = {
    subscribeSSE(ep) {
      calls.subscribe.push(ep);
      return Promise.reject(new Error('async subscribe failed'));
    },
    unsubscribeSSE(id) {
      calls.unsubscribe.push(id);
      return Promise.resolve();
    },
  };
  return { sdk, calls };
}

async function testSaveAndUnsubscribe(K, window) {
  const { sdk, calls } = makeSdk();
  window.AstrBotPluginPage = sdk;
  const statuses = [];
  const handle = K.subscribeSSE('events', () => {}, (status) => statuses.push(status));
  await Promise.resolve();
  assert.equal(calls.subscribe.length, 1);
  assert.equal(calls.unsubscribe.length, 0);
  handle.close();
  await Promise.resolve();
  assert.deepEqual(calls.unsubscribe, ['sub-1']);
  handle.close();
  await Promise.resolve();
  assert.equal(calls.unsubscribe.length, 1, 'close must be idempotent');
  void statuses;
}

async function testCloseBeforeResolve(K, window) {
  const { sdk, calls } = makeSdk();
  window.AstrBotPluginPage = sdk;
  let resolveSubscription;
  const controlledSdk = {
    ...sdk,
    subscribeSSE(ep, handlers) {
      return new Promise((resolve) => { resolveSubscription = () => resolve('pending-sub'); });
    },
  };
  window.AstrBotPluginPage = controlledSdk;
  const handle = K.subscribeSSE('events', () => {});
  handle.close();
  assert.equal(calls.unsubscribe.length, 0, 'cannot unsubscribe before an ID exists');
  resolveSubscription();
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(calls.unsubscribe, ['pending-sub']);
}

async function testReconnectUnsubscribes(K, window) {
  const { sdk, calls } = makeSdk();
  window.AstrBotPluginPage = sdk;
  const handle = K.connectSSEWithReconnect('events', () => {}, undefined, {
    baseDelay: 1,
    maxDelay: 1,
    heartbeatTimeout: 1000,
  });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(calls.subscribe.length, 1);
  handle.reconnect();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(calls.subscribe.length, 2);
  assert.deepEqual(calls.unsubscribe, ['sub-1'], 'reconnect must close the previous subscription');
  handle.close();
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(calls.unsubscribe, ['sub-1', 'sub-2']);
  assert.equal(calls.subscribe.length, 2, 'close must not reconnect');
}

async function testAsyncSubscribeFailure(K, window) {
  const { sdk, calls } = makeRejectingSdk();
  window.AstrBotPluginPage = sdk;
  const statuses = [];
  const handle = K.subscribeSSE('events', () => {}, (status) => statuses.push(status));
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(calls.subscribe.length, 1);
  assert.deepEqual(statuses, ['disconnected']);
  assert.doesNotThrow(() => handle.close());
  await Promise.resolve();
  assert.deepEqual(calls.unsubscribe, []);
}

async function testSyncUnsubscribeFailure(K, window) {
  const { sdk, calls } = makeSdk({ unsubscribeError: new Error('sync unsubscribe failed') });
  window.AstrBotPluginPage = sdk;
  const handle = K.subscribeSSE('events', () => {});
  await Promise.resolve();
  handle.close();
  assert.deepEqual(calls.unsubscribe, ['sub-1']);
}

async function testAsyncUnsubscribeFailure(K, window) {
  const { sdk, calls } = makeSdk({ unsubscribeAsyncError: new Error('async unsubscribe failed') });
  window.AstrBotPluginPage = sdk;
  const handle = K.subscribeSSE('events', () => {});
  await Promise.resolve();
  handle.close();
  assert.deepEqual(calls.unsubscribe, ['sub-1']);
  await Promise.resolve();
  await Promise.resolve();
}

async function testStaleCallbacksAfterCloseAndReconnect(K, window) {
  const { sdk, calls } = makeSdk();
  window.AstrBotPluginPage = sdk;
  const statuses = [];
  const handle = K.connectSSEWithReconnect('events', () => {}, (status) => statuses.push(status), {
    baseDelay: 1,
    maxDelay: 1,
    heartbeatTimeout: 1000,
  });
  await Promise.resolve();
  await Promise.resolve();
  const firstHandlers = calls.handlers[0];
  handle.reconnect();
  await Promise.resolve();
  await Promise.resolve();
  const secondHandlers = calls.handlers[1];

  firstHandlers.onOpen();
  firstHandlers.onError();
  assert.deepEqual(statuses, ['connecting', 'connecting'], 'stale handlers must not report status');

  handle.close();
  secondHandlers.onOpen();
  secondHandlers.onError();
  assert.deepEqual(statuses, ['connecting', 'connecting'], 'closed handlers must not report status');
}

async function testPendingSubscriptionReconnectResolveOutOfOrder(K, window) {
  const resolvers = [];
  const calls = { subscribe: [], unsubscribe: [], handlers: [] };
  window.AstrBotPluginPage = {
    subscribeSSE(ep, handlers) {
      calls.subscribe.push(ep);
      calls.handlers.push(handlers);
      return new Promise((resolve) => { resolvers.push(resolve); });
    },
    unsubscribeSSE(id) {
      calls.unsubscribe.push(id);
      return Promise.resolve();
    },
  };
  const handle = K.connectSSEWithReconnect('events', () => {}, undefined, {
    baseDelay: 1,
    maxDelay: 1,
    heartbeatTimeout: 1000,
  });
  await Promise.resolve();
  handle.reconnect();
  await Promise.resolve();
  assert.equal(resolvers.length, 2);

  resolvers[1]('new-sub');
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(calls.unsubscribe, [], 'the active pending subscription must remain subscribed');

  handle.close();
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(calls.unsubscribe, ['new-sub']);

  resolvers[0]('old-sub');
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(calls.unsubscribe, ['new-sub', 'old-sub'], 'late old subscription must be cleaned up');
}

async function testSyncSubscribeFailure(K, window) {
  const { sdk, calls } = makeSdk({ subscribeError: new Error('sync subscribe failed') });
  window.AstrBotPluginPage = sdk;
  const statuses = [];
  const handle = K.subscribeSSE('events', () => {}, (status) => statuses.push(status));
  assert.equal(calls.subscribe.length, 1);
  assert.deepEqual(statuses, ['disconnected']);
  handle.close();
  await Promise.resolve();
  assert.deepEqual(calls.unsubscribe, []);
}

async function testHeartbeatReconnect(K, window) {
  const { sdk, calls } = makeSdk();
  window.AstrBotPluginPage = sdk;
  const statuses = [];
  const handle = K.connectSSEWithReconnect('events', () => {}, (status) => statuses.push(status), {
    baseDelay: 10,
    maxDelay: 20,
    heartbeatTimeout: 5,
  });
  await Promise.resolve();
  assert.equal(calls.subscribe.length, 1);
  K.__clock.tick(5);
  assert.deepEqual(calls.unsubscribe, ['sub-1']);
  assert.ok(statuses.includes('disconnected'));
  K.__clock.tick(10);
  await Promise.resolve();
  assert.equal(calls.subscribe.length, 2);
  handle.close();
  await Promise.resolve();
  assert.deepEqual(calls.unsubscribe, ['sub-1', 'sub-2']);
  K.__clock.tick(1000);
  assert.equal(calls.subscribe.length, 2);
  assert.equal(K.__clock.size(), 0, 'close must clear all reconnect and heartbeat timers');
}

const cases = {
  'saves subscription ID and unsubscribes exactly once': testSaveAndUnsubscribe,
  'unsubscribes when close happens before subscribe resolves': testCloseBeforeResolve,
  'reconnect closes the old subscription and final close closes the new one': testReconnectUnsubscribes,
  'handles async subscribe rejection without an unhandled rejection': testAsyncSubscribeFailure,
  'handles sync unsubscribe rejection': testSyncUnsubscribeFailure,
  'handles async unsubscribe rejection without an unhandled rejection': testAsyncUnsubscribeFailure,
  'ignores stale status callbacks after close and during reconnect': testStaleCallbacksAfterCloseAndReconnect,
  'handles out-of-order pending subscriptions across reconnect': testPendingSubscriptionReconnectResolveOutOfOrder,
  'handles synchronous subscribe throw without an unhandled error': testSyncSubscribeFailure,
  'reconnects automatically when the heartbeat times out': testHeartbeatReconnect,
};

(async () => {
  for (const source of sources) {
    for (const [name, test] of Object.entries(cases)) {
      const K = loadScript(source);
      const window = K.__testWindow;
      assert.equal(typeof K.subscribeSSE, 'function', `${source.label} lacks subscribeSSE`);
      assert.equal(typeof K.connectSSEWithReconnect, 'function', `${source.label} lacks reconnect helper`);
      await test(K, window);
      console.log(`ok - ${source.label}: ${name}`);
    }
  }
  console.log(`SSE lifecycle regression tests passed (${sources.length * Object.keys(cases).length} cases)`);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});

process.on('unhandledRejection', (error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
