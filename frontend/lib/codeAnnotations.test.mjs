import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const compiled = ts.transpileModule(readFileSync(new URL("./codeAnnotations.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText;
const exports = {};
runInNewContext(compiled, { exports });
const { placeCodeAnnotations } = exports;
const visible = { path: "src/layers.py", start: 20, end: 60, total: 100 };
const step = (line_ref, text = "解释 **当前操作**，不是原仓库代码。") => ({ line_ref, text });

test("讲解映射到单行与范围起始行，相同行号保留每段原文和独立标识", () => {
  const steps = [step("src/layers.py:37"), step("src/layers.py:40-43"), step("src/layers.py:37")];
  const before = JSON.stringify(steps);
  const { byLine, unplaced } = placeCodeAnnotations(steps, visible);
  assert.deepEqual([...byLine.keys()], [37, 40]);
  assert.equal(byLine.get(37).length, 2);
  assert.notEqual(byLine.get(37)[0].id, byLine.get(37)[1].id);
  assert.equal(byLine.get(40)[0].reference.end, 43);
  assert.equal(byLine.get(40)[0].text, steps[1].text);
  assert.equal(unplaced.length, 0);
  assert.equal(JSON.stringify(steps), before);
});

test("同名但不同路径的文件不混放，换文件后使用同一份讲解重新定位", () => {
  const steps = [step("src/layers.py:37"), step("other/layers.py:37", "另一文件的讲解")];
  const first = placeCodeAnnotations(steps, visible);
  assert.equal(first.byLine.get(37).length, 1);
  assert.equal(first.unplaced[0].reference.path, "other/layers.py");
  const second = placeCodeAnnotations(steps, { ...visible, path: "other/layers.py" });
  assert.equal(second.byLine.get(37)[0].text, "另一文件的讲解");
  assert.equal(second.unplaced[0].reference.path, "src/layers.py");
});

test("跨视窗范围贴在首个可见行，视窗外讲解保留真实跳转位置", () => {
  const { byLine, unplaced } = placeCodeAnnotations([
    step("src/layers.py:15-25"), step("src/layers.py:55-70"), step("src/layers.py:5-10"), step("src/layers.py:80"),
  ], visible);
  assert.deepEqual([...byLine.keys()], [20, 55]);
  assert.equal(byLine.get(20)[0].reference.start, 15);
  assert.deepEqual(unplaced.map((item) => item.reference.start).join(","), "5,80");
  assert.ok(unplaced.every((item) => item.notice === "不在当前代码范围"));
});

test("非法、倒序、零行号及不安全整数引用不会丢失或伪造代码行", () => {
  const refs = ["bad reference", ":3", " :3", "src/layers.py:0", "src/layers.py:8-3", "src/layers.py:9007199254740992"];
  const { byLine, unplaced } = placeCodeAnnotations(refs.map((ref) => step(ref)), visible);
  assert.equal(byLine.size, 0);
  assert.equal(unplaced.length, refs.length);
  unplaced.forEach((item, index) => {
    assert.equal(item.line_ref, refs[index]);
    assert.equal(item.reference, null);
    assert.ok(item.notice);
  });
});

test("超出文件总行数的引用明确报出，不能跳转到截断后的错误位置", () => {
  const { byLine, unplaced } = placeCodeAnnotations([step("src/layers.py:99-105")], visible);
  assert.equal(byLine.size, 0);
  assert.equal(unplaced[0].reference, null);
  assert.equal(unplaced[0].notice, "引用行号超出文件范围");
});

test("未打开文件时保留讲解，空讲解不产生虚构注释", () => {
  const pending = placeCodeAnnotations([step("src/layers.py:37")], null);
  assert.equal(pending.unplaced[0].reference.start, 37);
  assert.equal(pending.unplaced[0].notice, "尚未打开对应文件");
  const empty = placeCodeAnnotations(undefined, visible);
  assert.equal(empty.byLine.size, 0);
  assert.equal(empty.unplaced.length, 0);
});

test("模型给的讲解字段残缺时不崩页面：缺 line_ref / 非字符串 / 空条目都保留为无法定位", () => {
  const malformed = [{ text: "缺 line_ref" }, { line_ref: 37, text: "行号是数字" }, null, { line_ref: "src/layers.py:37", text: 42 }];
  const { byLine, unplaced } = placeCodeAnnotations(malformed, visible);
  assert.equal(unplaced.length, 3);
  assert.ok(unplaced.every((item) => item.reference === null && item.notice === "引用格式无法定位"));
  assert.equal(byLine.get(37)[0].text, "42");
  assert.equal(placeCodeAnnotations("不是数组", visible).unplaced.length, 0);
});
