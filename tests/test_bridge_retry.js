import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

// 回归：bridge 的「限流/连接超时类重试 → model_access 即时提醒」识别与计数。
// 真实事故（2026-09-11）：会话 session-b5b4f120… 的 llm/retry 连续 5 次
// errorCode=TIMEOUT（"upstream response headers timed out before streaming
// started"）后恢复正常，桌宠零提醒——旧 isModelAccessError 只认 429/限流码，
// TIMEOUT 类连续重试不进 noteRetryConnection 计数，永远写不出 model_access
// 事件（而 turn 正常 completed 也不会写 execution/failed，两条提醒路径都哑火）。
// 修复后：连接/超时类重试在达到 RETRY_EVENT_THRESHOLD 时同样产出 model_access，
// 由桌宠端既有模型访问失败弹窗路径呈现。
// 测试直接驱动与生产事件路径相同的原子函数（isModelAccessError /
// noteRetryConnection / resetRetryConnection），不启动 DSH 宿主。

const here = path.dirname(fileURLToPath(import.meta.url));
const bridgeDir = path.resolve(here, "../integrations/dsh-pet-bridge");

let retry;
test.before(async () => {
  const bridge = await import(pathToFileURL(path.join(bridgeDir, "index.js")));
  retry = bridge.__retryTest;
  assert.ok(retry, "bridge must export __retryTest");
  assert.equal(typeof retry.isModelAccess, "function", "__retryTest 需导出 isModelAccess");
});

test("阈值=5：同一 session 连续重试恰好第 5 次触发一次", () => {
  const key = "session-conn-1";
  retry.reset(key);
  let fired = 0;
  for (let i = 1; i <= 6; i++) {
    if (retry.note(key)) fired += 1; // note 只在第 5 次返回 true（notified 后不再报）
  }
  assert.equal(fired, 1, "连续 5 次达到阈值只提醒一次，之后不再轰炸");
  retry.reset(key);
});

test("isModelAccess 识别限流类（code 与 message 双通道）", () => {
  assert.equal(retry.isModelAccess("RATE_LIMIT", "429: too many requests"), true);
  assert.equal(retry.isModelAccess("429", ""), true);
  assert.equal(retry.isModelAccess("TOO_MANY_REQUESTS", ""), true);
  assert.equal(retry.isModelAccess("", "429 too many requests"), true);
  assert.equal(retry.isModelAccess("", "rate limit exceeded"), true);
});

test("isModelAccess 识别连接/超时类（code 命中）", () => {
  for (const code of ["TIMEOUT", "REQUEST_TIMEOUT", "UPSTREAM_TIMEOUT", "ETIMEDOUT",
                      "ESOCKETTIMEDOUT", "ECONNRESET", "ECONNABORTED", "ECONNREFUSED",
                      "EPIPE", "EAI_AGAIN", "ENETUNREACH", "EHOSTUNREACH", "NETWORK_ERROR"]) {
    assert.equal(retry.isModelAccess(code, ""), true, `${code} 应识别为模型访问失败`);
  }
});

test("isModelAccess 识别连接/超时类（消息命中，真实事故原文）", () => {
  // 真实事故：errorCode=TIMEOUT + 该错误正文
  assert.equal(
    retry.isModelAccess("TIMEOUT",
      "upstream stream read failed before completion: upstream response headers timed out before streaming started"),
    true,
  );
  // 错误码缺失、仅消息含连接断词
  assert.equal(retry.isModelAccess("", "upstream connection reset by peer"), true);
  assert.equal(retry.isModelAccess("", "socket hang up"), true);
});

test("isModelAccess 不误认无关错误", () => {
  assert.equal(retry.isModelAccess("bad_response_status_code", "AI 服务返回 503"), false);
  assert.equal(retry.isModelAccess("server_error", "upstream boom"), false);
  assert.equal(retry.isModelAccess("", "permission denied: cannot open config.json"), false);
});

test("连续 5 次 TIMEOUT 重试走 note 计数即阈值（用户观察到的 5 次场景）", () => {
  // 生产的 llm/retry 分支 = isModelAccessError 判定 + noteRetryConnection 计数；
  // TIMEOUT 属于模型访问失败后，5 次连续重试必须恰好触发一次提醒。
  const key = "session-b5b4f120-8eac-4d33-b9c7-69e39a6e6302";
  retry.reset(key);
  const MSG = "upstream stream read failed before completion: upstream response headers timed out before streaming started";
  let firedAt = 0;
  for (let i = 1; i <= 5; i++) {
    if (!retry.isModelAccess("TIMEOUT", MSG)) {
      assert.fail(`第 ${i} 次 TIMEOUT 重试应被识别为模型访问失败`);
    }
    if (retry.note(key)) firedAt = i;
  }
  assert.equal(firedAt, 5, "第 5 次重试时应产出 model_access 提醒");
  retry.reset(key);
});