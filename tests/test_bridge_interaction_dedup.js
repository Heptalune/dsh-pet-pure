// 问题写盘去重键的契约（2026-10 减法后：审批帧不再由桥接写出，只剩问题一族）。
//
// question/requested 由 assistant/message 的 tool-call 块与独立 tool/call 事件
// 双通道产生，`_interactionDedupKeys` 按 callId（复合 sessionId 隔离）去重——
// 同一条问题 8s 内只落一条记录，跨会话同 callId 不互吞。
// 直接从源码提取并求值纯函数（与仓库既有 bridge 契约测试一致）。
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.resolve(here, "../integrations/dsh-pet-bridge/index.js");
const source = fs.readFileSync(sourcePath, "utf8");

function loadInteractionDedupKeys() {
  const match = source.match(/function _interactionDedupKeys\(extra\) \{[\s\S]*?\n\}/);
  assert.ok(match, "index.js 应定义 _interactionDedupKeys");
  return new Function(`${match[0]}; return _interactionDedupKeys;`)();
}

test("question dedup key carries callId scoped by session", () => {
  const keysFor = loadInteractionDedupKeys();
  const keys = keysFor({ event: "question/requested", callId: "call-1", sessionId: "sess-a" });
  assert.ok(keys.includes("qu:call:sess-a:call-1"), "callId 去重键必须带 sessionId 隔离");
});

test("same callId in different sessions never shares a dedup key", () => {
  const keysFor = loadInteractionDedupKeys();
  const a = keysFor({ event: "question/requested", callId: "call-1", sessionId: "sess-a" });
  const b = keysFor({ event: "question/requested", callId: "call-1", sessionId: "sess-b" });
  assert.deepEqual(a.filter((k) => b.includes(k)), [], "跨会话同 callId 不得共享去重键");
});

test("approval records no longer produce dedup keys (mux relay removed)", () => {
  const keysFor = loadInteractionDedupKeys();
  // 减法后桥接不写 approval/request（mux 中继已删）；即便旧桩/手写记录流入，
  // 也不再生成审批去重键。
  assert.deepEqual(
    keysFor({ event: "approval/request", approvalId: "ap-1", sessionId: "sess-a", tool: "exec", command: "ls" }),
    [],
  );
});

test("unrelated events produce no dedup keys", () => {
  const keysFor = loadInteractionDedupKeys();
  assert.deepEqual(keysFor({ event: "tool/call", callId: "c-1" }), []);
});
