import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

test("所有生成请求传递解析语言，供应商配置保持原来的协议", async () => {
  const compiled = ts.transpileModule(readFileSync(new URL("./api.ts", import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 },
  }).outputText;
  const requests = [];
  const api = {};
  runInNewContext(compiled, { exports: api, process: { env: {} }, FormData,
    fetch: async (url, options) => {
      requests.push({ url, ...options });
      return { ok: true, json: async () => ({}) };
    },
  });
  const provider = { protocol: "openai-compatible", api_key: "mock-key", model: "mock" };
  const file = new File(["fixture"], "paper.pdf", { type: "application/pdf" });
  await api.createRun(file, provider, "en");
  assert.equal(requests.pop().body.get("output_language"), "en");
  await api.startRecon("run", provider, "en");
  await api.startLocate("run", provider, "https://github.com/example/repo", ["inn-1"], "en");
  await api.postChat("run", { provider, message: "继续", output_language: "zh-CN" });
  assert.deepEqual(requests.map(({ body }) => JSON.parse(body).output_language), ["en", "en", "zh-CN"]);
  for (const { body } of requests) assert.deepEqual(JSON.parse(body).provider, provider);
});
