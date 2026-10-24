// question/requested 写盘去重与 identity 管理（tool/call + assistant/message
// 双通道，2026-10 减法后 mux 中继已删，问题身份只剩 callId 一族）。
//
// 桌宠端问题气泡靠 callId 与兜底 question/resolved 配对关闭；register 幂等
// （同 sessionId|callId 复合键不重复登记）与 forget（resolved 后清理）是
// 「问题不重复弹、气泡关得上」的两个支点。直接驱动导出的纯函数，不启动 DSH 宿主。
import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const bridgeDir = path.resolve(here, "../integrations/dsh-pet-bridge");

let q;
test.before(async () => {
  const bridge = await import(pathToFileURL(path.join(bridgeDir, "index.js")));
  q = bridge.__questionTest;
  assert.ok(q, "bridge must export __questionTest");
});

test.beforeEach(() => {
  q.pendingQuestionCallIds.clear();
});

test("register 幂等：同会话同 callId 不重复登记", () => {
  assert.equal(q.registerQuestionCall("call-9", "sess-1"), true);
  assert.equal(q.registerQuestionCall("call-9", "sess-1"), false, "重复登记必须被拒（去重）");
});

test("复合键按会话隔离：同 callId 不同会话各自独立", () => {
  assert.equal(q.registerQuestionCall("call-9", "sess-1"), true);
  assert.equal(q.registerQuestionCall("call-9", "sess-2"), true, "跨会话不得误去重");
});

test("空 callId 不登记", () => {
  assert.equal(q.registerQuestionCall("", "sess-1"), false);
});

test("forget 清理复合键，清完可重新登记", () => {
  q.registerQuestionCall("call-9", "sess-1");
  q.forgetQuestionCall("call-9", "sess-1");
  assert.equal(q.pendingQuestionCallIds.size, 0);
  assert.equal(q.registerQuestionCall("call-9", "sess-1"), true, "forget 后必须能重新登记");
});

test("forget 只清本会话条目", () => {
  q.registerQuestionCall("call-9", "sess-1");
  q.registerQuestionCall("call-9", "sess-2");
  q.forgetQuestionCall("call-9", "sess-1");
  assert.equal(q.pendingQuestionCallIds.size, 1, "sess-2 的条目必须保留");
});
