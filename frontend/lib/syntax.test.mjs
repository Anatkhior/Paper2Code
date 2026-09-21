import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { runInNewContext } from "node:vm";
import ts from "typescript";

// 使用项目已有编译器，测试同样可运行在 README 支持的 Node 20 上。
const compiled = ts.transpileModule(readFileSync(new URL("./syntax.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true },
}).outputText;
const exports = {};
runInNewContext(compiled, { require: createRequire(import.meta.url), exports });
const { highlightCode } = exports;

const restore = (path, source) => highlightCode(path, source).lines.map((line) => line.map((part) => part.text).join("")).join("\n");

test("保留空行、缩进、制表符与末尾换行", () => {
  const source = 'def forward(x):\n\n\treturn x * 0.5\n';
  assert.equal(restore("layers.py", source), source);
  const result = highlightCode("layers.py", source);
  assert.equal(result.language, "python");
  for (const type of ["keyword", "function", "number"]) {
    assert.ok(result.lines.flat().some((part) => part.types.includes(type)));
  }
});

test("Python 多行字符串与 TypeScript 多行注释跨行保留类型", () => {
  const python = highlightCode("a.py", 'x = """first\nsecond\nthird"""');
  assert.ok(python.lines[1].every((part) => part.types.includes("string")));
  const ts = highlightCode("a.ts", '/* first\nsecond */\nconst x: number = 3;');
  assert.ok(ts.lines[1].every((part) => part.types.includes("comment")));
  assert.ok(ts.lines[2].some((part) => part.types.includes("keyword")));
});

test("HTML 字面量是文本 token，未知语言明确返回纯文本", () => {
  const source = '<script>alert("x")</script>\n';
  assert.equal(restore("a.html", source), source);
  const unknown = highlightCode("a.unknown", source);
  assert.equal(unknown.language, "纯文本");
  assert.ok(unknown.lines.flat().every((part) => part.types.length === 0));
  assert.equal(restore("a.unknown", source), source);
});
