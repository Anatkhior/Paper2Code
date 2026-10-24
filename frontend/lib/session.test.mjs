import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const exports = {};
runInNewContext(ts.transpileModule(readFileSync(new URL("./session.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 },
}).outputText, { exports });
const { appendEvent, providerChange } = exports;

test("恢复快照后忽略旧阶段的结束与重复事件，保留旧结论", () => {
  const saved = { event_cursor: 40, events: [], active: true, findings: [{ id: "old" }], turns: [{ text: "old answer" }] };
  assert.equal(appendEvent(saved, { id: 20, type: "run_end", data: {} }), saved);
  const next = appendEvent(saved, { id: 41, type: "chat_reply", data: { message: "new" } });
  assert.equal(next.event_cursor, 41);
  assert.equal(next.findings, saved.findings);
  assert.equal(appendEvent(next, { id: 41, type: "chat_reply", data: {} }), next);
  assert.equal(appendEvent(next, { id: 42, type: "run_end", data: {} }).active, false);
});

test("同端点换模型保留密钥；切换端点或协议清除旧密钥", () => {
  const before = { protocol: "openai-compatible", base_url: "https://a.invalid/v1/", api_key: "old-key", model: "a" };
  assert.equal(providerChange(before, { ...before, model: "b" }).api_key, "old-key");
  assert.equal(providerChange(before, { ...before, base_url: "https://a.invalid/v1" }).api_key, "old-key");
  assert.equal(providerChange(before, { ...before, base_url: "https://b.invalid/v1" }).api_key, "");
  assert.equal(providerChange(before, { ...before, protocol: "anthropic" }).api_key, "");
});
