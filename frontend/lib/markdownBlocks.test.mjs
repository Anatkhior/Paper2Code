import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const compiled = ts.transpileModule(readFileSync(new URL("./markdownBlocks.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText;
const exports = {};
runInNewContext(compiled, { exports });
const { parseBlocks } = exports;

test("标题里混入 U+2028/U+2029 时照常解析，不会卡死（曾经整个标签页死循环）", () => {
  const blocks = parseBlocks("## 标题 a b\n正文\n### 另一个 标题");
  assert.deepEqual([...blocks.map((block) => block.kind)], ["h", "p", "h"]);
  assert.equal(blocks[0].text, "标题 a b");
});

test("任何一行都会被某种块认领：解析总能结束，内容不丢", () => {
  const tricky = ["#\u2028", "> ", "- ", "1. ", "#######x", "\u2029", "## \u2029"].join("\n");
  const blocks = parseBlocks(tricky);
  const texts = blocks.flatMap((block) => (block.kind === "h" ? [block.text] : [...(block.lines ?? block.items ?? [])]));
  assert.ok(texts.includes("#######x"));
  assert.equal(blocks.at(-1).kind, "h");
});

test("常规写法的切分不变：标题、列表、引用、代码块、段落", () => {
  const blocks = parseBlocks("# 标题\n\n- a\n- b\n\n1. x\n2) y\n\n> 引用\n\n```py\ncode\n```\n第一行\n第二行");
  assert.deepEqual([...blocks.map((block) => block.kind)], ["h", "ul", "ol", "quote", "code", "p"]);
  assert.deepEqual([...blocks[5].lines], ["第一行", "第二行"]);
});
