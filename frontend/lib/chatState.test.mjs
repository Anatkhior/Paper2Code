import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

function load(name) {
  const compiled = ts.transpileModule(readFileSync(new URL(name, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 },
  }).outputText;
  const exports = {};
  runInNewContext(compiled, { exports });
  return exports;
}
const { chatState, chatTurnsFromEvents } = load("./chatState.ts");
const { migrateStorage, PROVIDER_STORAGE_KEY, TIMELINE_STORAGE_KEY } = load("./providerStorage.ts");
const event = (id, type, data = {}) => ({ id, type, data, ts: id });

test("旧阶段结束不影响新追问；查阅开场白不冒充正文", () => {
  const events = [event(1, "run_end"), event(2, "chat_user", { message: "问题" }),
    event(3, "step_start", { mode: "research" }), event(4, "assistant_text", { delta: "下面给你答案" }),
    event(5, "tool_call", { tool: "get_page_text" })];
  const state = chatState(events);
  assert.equal(state.streaming, "");
  assert.equal(state.ended, false);
  assert.equal(state.userId, 2);
  assert.match(state.status, /get_page_text/);
});

test("无工具正文流完整保留，chat_reply 到达后只展示一次", () => {
  const answer = "完整回答。\n\n".repeat(1500);
  const events = [event(1, "chat_user"), event(2, "step_start", { mode: "answer" }),
    event(3, "assistant_text", { delta: answer })];
  assert.equal(chatState(events).streaming, answer);
  events.push(event(4, "chat_reply", { message: answer, status: "ok", partial: false }));
  const state = chatState(events);
  assert.equal(state.streaming, "");
  assert.equal(state.reply.text, answer);
  assert.equal(chatTurnsFromEvents(events).length, 2);
});

test("没有 chat_reply 的结束也解除 busy，限流/取消明确显示", () => {
  const events = [event(1, "chat_user"), event(2, "llm_retry", { delay_seconds: 7.5 })];
  assert.match(chatState(events).status, /7.5/);
  events.push(event(3, "run_end", { status: "cancelled", stopped_reason: "用户取消" }));
  assert.equal(chatState(events).ended, true);
  assert.match(chatState(events).status, /未收到完整回答/);
  assert.equal(chatState(events).reason, "用户取消");
});

test("新的问题不受旧回答状态影响，部分答案保留中断状态", () => {
  const events = [event(1, "chat_user"), event(2, "chat_reply", { message: "旧答案" }), event(3, "run_end"),
    event(4, "chat_user"), event(5, "chat_reply", { message: "部分", status: "failed", partial: true })];
  const state = chatState(events);
  assert.equal(state.reply.text, "部分");
  assert.match(state.status, /未完整交付/);
});

function store(entries) {
  const data = new Map(entries);
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) };
}

test("旧设置迁移时不擅自记住密钥，时间线偏好保留", () => {
  const storage = store([["paperlens.provider.v1", JSON.stringify({ model: "m", api_key: "secret", remember_key: false })],
    ["paperlens.timelineOpen", "0"]]);
  migrateStorage(storage);
  const saved = JSON.parse(storage.getItem(PROVIDER_STORAGE_KEY));
  assert.equal(saved.api_key, "");
  assert.equal(saved.model, "m");
  assert.equal(storage.getItem(TIMELINE_STORAGE_KEY), "0");
});

test("明确选择记住的密钥可迁移，但新键始终优先", () => {
  const storage = store([["paperlens.provider.v1", JSON.stringify({ model: "old", api_key: "secret", remember_key: true })]]);
  migrateStorage(storage);
  assert.equal(JSON.parse(storage.getItem(PROVIDER_STORAGE_KEY)).api_key, "secret");
  storage.setItem(PROVIDER_STORAGE_KEY, JSON.stringify({ model: "new" }));
  migrateStorage(storage);
  assert.equal(JSON.parse(storage.getItem(PROVIDER_STORAGE_KEY)).model, "new");
});

test("损坏的旧设置不复制到新键", () => {
  const storage = store([["paperlens.provider.v1", "not json"]]);
  migrateStorage(storage);
  assert.equal(storage.getItem(PROVIDER_STORAGE_KEY), null);
});
