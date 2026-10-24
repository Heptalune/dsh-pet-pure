// 减法回归（2026-10）：看门狗控制队列与 mux 中继已整段删除。
//
// 原 test_bridge_root_control.js 钉的是 resolveControlRoot（子代理 → 根会话
// 归一），随控制队列一并退役。本文件改为钉「不得复活」静态闸：
// 控制队列 / mux 中继 / 看门狗诊断的标识符不得在 index.js 中再出现，
// 对应测试出口 __controlTest / __messageTest 不得再导出。
//（verify_import.mjs 有同族禁令；本文件进 node --test 套件，防绕过冒烟脚本。）
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const bridgeDir = path.resolve(here, "../integrations/dsh-pet-bridge");
const source = fs.readFileSync(path.join(bridgeDir, "index.js"), "utf8");

test("控制队列机制不存在于源码", () => {
  for (const banned of [
    "startControlQueue", "handleControlRequest", "runBridgeDiagnosis",
    "resolveControlRoot", "writeControlResponse", "watchdog-request-",
    "CONTROL_POLL_MS", "waitAgentIdle",
  ]) {
    assert.ok(!source.includes(banned), `控制队列已退役，不得复活: ${banned}`);
  }
});

test("mux WebSocket 中继不存在于源码", () => {
  for (const banned of ["muxConnect", "muxSocket", "events.mux", "WebSocket"]) {
    assert.ok(!source.includes(banned), `mux 中继已退役，不得复活: ${banned}`);
  }
});

test("退役的测试出口不再导出", async () => {
  const bridge = await import(pathToFileURL(path.join(bridgeDir, "index.js")).href);
  assert.equal(bridge.__controlTest, undefined, "__controlTest 随控制队列删除");
  assert.equal(bridge.__messageTest, undefined, "__messageTest 随诊断/steer 删除");
  // 存活机制的合同出口仍在（防误删）
  assert.ok(bridge.__retryTest && bridge.__hardFailureTest && bridge.__questionTest);
});
