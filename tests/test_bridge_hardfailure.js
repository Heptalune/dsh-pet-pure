import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

// 回归：bridge 硬失败判定（execution/failed）不再误报。
// 规则（与 integrations/dsh-pet-bridge/index.js 的 __hardFailureTest 对齐）：
//   - turn/end 只有 reason.kind === "error" 才可能判硬失败；
//     completed/aborted/blocked/max-tokens/缺失 → 一律不写。
//   - 重试计数「恢复即清零」：assistant/message、tool/call、成功的 tool/result
//     都把连续重试归零，已恢复的抖动绝不累加成「重试耗尽」。
// 历史误报场景：一次 turn 里模型重试 6 次但每次都已恢复、turn 正常 completed，
// 旧逻辑 retries 只增不减 + 不看 reason → 误写 execution/failed。
// 测试直接用与生产事件路径相同的原子函数（resetTurnStats / noteRetry /
// recovery / noteToolResult / decideTurnEnd）驱动单个 turn 统计对象。

const here = path.dirname(fileURLToPath(import.meta.url));
const bridgeDir = path.resolve(here, "../integrations/dsh-pet-bridge");

let hard;
test.before(async () => {
  const bridge = await import(pathToFileURL(path.join(bridgeDir, "index.js")));
  hard = bridge.__hardFailureTest;
  assert.ok(hard, "bridge must export __hardFailureTest");
});

function freshTurn() {
  // 与 index.js turn/start 走同一条 resetTurnStats 路径
  return hard.resetTurnStats({
    retries: 0, hadSuccess: false, hadFailure: false,
    lastErrorCode: "", lastErrorMessage: "", lastRetryCode: "", turnActive: false,
  });
}

function errorReason(code = "RATE_LIMIT", message = "429: too many requests") {
  return { kind: "error", error: { code, message } };
}

test("正常完成（completed）绝不判失败——即使中途重试过 6 次且已恢复", () => {
  const st = freshTurn();
  for (let i = 0; i < 6; i++) hard.noteRetry(st, "bad_response_status_code");
  hard.recovery(st); // 模型随后成功产出 → 恢复即清零
  assert.equal(st.retries, 0);
  assert.equal(hard.decideTurnEnd({ kind: "completed" }, st), null);
});

test("历史误报场景：6 次重试均恢复 + turn 正常 completed → 不写 execution/failed", () => {
  const st = freshTurn();
  for (let i = 0; i < 6; i++) hard.noteRetry(st, "RATE_LIMIT");
  hard.recovery(st);            // 第 6 次后恢复
  hard.noteToolResult(st, true); // 工具成功，流程继续
  assert.equal(st.retries, 0);
  assert.equal(hard.decideTurnEnd({ kind: "completed" }, st), null);
});

test("completed + 工具失败无成功也不误报（agent 已正常收尾）", () => {
  const st = freshTurn();
  hard.noteToolResult(st, false, "EACCES", "permission denied");
  assert.equal(hard.decideTurnEnd({ kind: "completed" }, st), null);
});

test("真·连续重试耗尽 + turn 以 error 结尾 → 写 model_retry_exhausted 硬失败", () => {
  const st = freshTurn();
  for (let i = 0; i < 6; i++) hard.noteRetry(st, "bad_response_status_code");
  const out = hard.decideTurnEnd(errorReason("bad_response_status_code"), st);
  assert.ok(out, "应判定为硬失败");
  assert.equal(out.event, "execution/failed");
  assert.equal(out.failureType, "model_retry_exhausted");
  assert.equal(out.retryExhausted, true);
  assert.equal(out.retries, 6);
  assert.equal(out.errorCode, "bad_response_status_code"); // 用最近一次重试码
});

test("重试不足阈值 + error 结尾 → 不写（非耗尽错误不按硬失败提醒）", () => {
  const st = freshTurn();
  hard.noteRetry(st, "RATE_LIMIT");
  hard.noteRetry(st, "RATE_LIMIT");
  assert.equal(hard.decideTurnEnd(errorReason("RATE_LIMIT"), st), null);
});

test("工具最终失败 + turn 以 error 结尾 → 写 tool_failed 硬失败（带工具错误码）", () => {
  const st = freshTurn();
  hard.noteToolResult(st, false, "EACCES", "permission denied");
  const out = hard.decideTurnEnd(errorReason("TOOL_EXECUTION_FAILED"), st);
  assert.ok(out, "应判定为硬失败");
  assert.equal(out.failureType, "tool_failed");
  assert.equal(out.retryExhausted, false);
  assert.equal(out.errorCode, "EACCES");
});

test("恢复清零后的连续重试仍按真实连续次数判定", () => {
  const st = freshTurn();
  // 先抖动：2 次重试后恢复
  hard.noteRetry(st, "RATE_LIMIT");
  hard.noteRetry(st, "RATE_LIMIT");
  hard.recovery(st);
  assert.equal(st.retries, 0);
  // 之后真·连续耗尽 5 次并以 error 收尾
  for (let i = 0; i < 5; i++) hard.noteRetry(st, "bad_response_status_code");
  const out = hard.decideTurnEnd(errorReason("bad_response_status_code"), st);
  assert.ok(out);
  assert.equal(out.failureType, "model_retry_exhausted");
  assert.equal(out.retries, 5);
});

test("非 error 结尾（aborted/blocked/max-tokens/缺失 reason）一律不判失败", () => {
  const st = freshTurn();
  for (let i = 0; i < 6; i++) hard.noteRetry(st, "RATE_LIMIT");
  assert.equal(hard.decideTurnEnd({ kind: "aborted", reason: { kind: "disposed" } }, st), null);
  assert.equal(hard.decideTurnEnd({ kind: "blocked" }, st), null);
  assert.equal(hard.decideTurnEnd({ kind: "max-tokens" }, st), null);
  assert.equal(hard.decideTurnEnd(null, st), null);
  assert.equal(hard.decideTurnEnd({ kind: "" }, st), null);
});

test("turn 未激活（无 turn/start）时不误判", () => {
  const st = freshTurn();
  st.turnActive = false;
  for (let i = 0; i < 6; i++) hard.noteRetry(st, "RATE_LIMIT");
  assert.equal(hard.decideTurnEnd(errorReason(), st), null);
});

test("错误正文不落盘：execution/failed 模型路径只带脱敏错误码", () => {
  const st = freshTurn();
  for (let i = 0; i < 4; i++) hard.noteRetry(st, "RATE_LIMIT");
  const out = hard.decideTurnEnd(errorReason("RATE_LIMIT", "429: a very long body ..."), st);
  assert.ok(out);
  assert.equal(out.errorMessage, "");        // 模型错误正文不写
  assert.equal(out.errorCode, "RATE_LIMIT"); // 只落码
});

test("execution/failed 不再带 source 字段（改名 failureType，避免与协议 source 撞名）", () => {
  const st = freshTurn();
  for (let i = 0; i < 4; i++) hard.noteRetry(st, "bad_response_status_code");
  const out = hard.decideTurnEnd(errorReason("bad_response_status_code"), st);
  assert.ok(out);
  assert.equal("source" in out, false, "旧字段 source 应彻底移除");
  assert.equal(out.failureType, "model_retry_exhausted");
  const st2 = freshTurn();
  hard.noteToolResult(st2, false, "EACCES", "denied");
  const out2 = hard.decideTurnEnd(errorReason("TOOL_EXECUTION_FAILED"), st2);
  assert.ok(out2);
  assert.equal("source" in out2, false);
  assert.equal(out2.failureType, "tool_failed");
});

test("turn/end 原因 kind 只取短字符串枚举", () => {
  assert.equal(hard.turnEndReasonKind({ data: { reason: { kind: "aborted" } } }), "aborted");
  assert.equal(hard.turnEndReasonKind({ data: { reason: { kind: 5 } } }), "");
  assert.equal(hard.turnEndReasonKind({ data: {} }), "");
  assert.equal(hard.turnEndReasonKind({ data: { reason: { kind: "x".repeat(40) } } }), "");
});
