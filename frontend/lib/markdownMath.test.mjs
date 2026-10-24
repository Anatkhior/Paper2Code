import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import * as markdownParser from "mdast-util-from-markdown";

const require = createRequire(import.meta.url);
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const cache = new Map();
function load(path) {
  if (cache.has(path)) return cache.get(path);
  const exports = {};
  const compiled = ts.transpileModule(readFileSync(path, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  runInNewContext(compiled, { exports, require: (name) => {
    if (name === "mdast-util-from-markdown") return markdownParser;
    if (!name.startsWith("@/")) return require(name);
    return load(resolve(root, name.slice(2) + (name.startsWith("@/components/") ? ".tsx" : ".ts")));
  } });
  cache.set(path, exports);
  return exports;
}
const Markdown = load(resolve(root, "components/Markdown.tsx")).default;
const render = (text, props = {}) => renderToStaticMarkup(createElement(Markdown, { text, ...props }));
const formulas = (html) => (html.match(/class="katex"/g) ?? []).length;

test("用户的中文算子、下标和四条独立公式实际渲染为 HTML 与 MathML", () => {
  const html = render(String.raw`\[x=\operatorname{拼接}\bigl(\phi(S),a,\xi_n\bigr)\]

\[y=\operatorname{MLP}\bigl(\operatorname{输入归一化}(x)\bigr)\]

\[d=y\odot\sigma_{\mathrm{out}}+\mu_{\mathrm{out}}\]

\[\hat S_{t+1}=\operatorname{状态更新}(S_t,d)\]`);
  assert.equal(formulas(html), 4);
  assert.equal((html.match(/class="katex-display"/g) ?? []).length, 4);
  assert.match(html, /<math/);
  assert.match(html, /拼/);
  assert.doesNotMatch(html, /katex-error/);
});

test("四种分隔符与粗体、列表、引用共存，美元金额与转义美元不被吃掉", () => {
  const html = render(String.raw`变量 \(x_1\)，**$y^2$**。
- 内积 $a\cdot b$

> 公式 \(z\)

$$E=mc^2$$

价格 $5 和 $10；转义 \$20。`);
  assert.equal(formulas(html), 5);
  assert.match(html, /<strong[^>]*><span class="katex"/);
  assert.match(html, /<ul/);
  assert.match(html, /<blockquote/);
  assert.match(html, /价格 \$5 和 \$10；转义 \$20/);
});

test("多行矩阵和对齐环境保留换行与反斜杠", () => {
  const html = render(String.raw`\[
\begin{aligned}
x &= \frac{1}{2} \\
y &= \begin{bmatrix}1 & 2 \\ 3 & 4\end{bmatrix}
\end{aligned}
\]`);
  assert.equal(formulas(html), 1);
  assert.doesNotMatch(html, /katex-error/);
  assert.match(html, /mtable/);
});

test("行内代码、代码围栏、嵌套围栏和缩进代码保持原文", () => {
  const text = '`\\(x\\)` 与 ``$x$``\n\n```latex\n\\[x\\]\n```\n\n~~~~text\n$$y$$\n~~~~\n\n    \\[z\\]\n\n- 示例\n\n  ```python\n  x = "$y$"\n  ```';
  const html = render(text);
  assert.equal(formulas(html), 0);
  assert.match(html, /\\\[x\\\]/);
  assert.match(html, /\\\[z\\\]/);
  assert.match(html, /\$\$y\$\$/);
});

test("完整公式在流式闭合后渲染，之前的每个前缀都能安全显示", () => {
  const answer = String.raw`先解释。\[y=\frac{x+1}{2}\] 然后继续。`;
  for (let i = 1; i < answer.length; i++) assert.doesNotThrow(() => render(answer.slice(0, i)));
  assert.equal(formulas(render(String.raw`先解释。\[y=\frac{x+1}{2}`)), 0);
  assert.equal(formulas(render("$$\ny=x")), 0);
  assert.equal(formulas(render(answer)), 1);
  assert.match(render(answer), /然后继续/);
});

test("不支持的 TeX 保留可见错误，不会让后续回答消失", () => {
  const html = render(String.raw`\[\frac{1}\] 后续内容`);
  assert.match(html, /katex-error/);
  assert.match(html, /后续内容/);
  assert.match(html, /title="ParseError/);
});

test("HTML、图片和公式中的外部执行能力保持禁用", () => {
  const html = render(String.raw`<img src=x onerror=alert(1)><script>alert(1)</script>

![远程图片](https://example.com/a.png)

[危险链接](javascript:alert%281%29)

\[\href{javascript:alert(1)}{x}\]

\[\includegraphics{https://example.com/a.png}\]`);
  assert.doesNotMatch(html, /<(?:script|img)\b|href="javascript:|src="https:/);
  assert.match(html, /&lt;img/);
});

test("行内宿主模式不产生块级标签或嵌套可点击链接", () => {
  const html = render("# 标题 $x$\n\n- 解释\n\n```py\na = 1\n```\n\n[链接](https://example.com)", { inline: true });
  assert.equal(formulas(html), 1);
  assert.doesNotMatch(html, /<(?:div|p|h[1-6]|pre|ul|ol|li|a)\b/);
});

test("裸公式字段和已有分隔符都渲染，宏定义不泄漏到下一公式", () => {
  assert.equal(formulas(render(String.raw`W=W_0+\frac{\alpha}{r}BA`, { math: true })), 1);
  assert.equal(formulas(render(String.raw`\[W=W_0\]`, { math: true })), 1);
  render(String.raw`\[\gdef\privateMacro{x}\privateMacro\]`);
  assert.match(render(String.raw`\[\privateMacro\]`), /color:#cc0000/);
});

test("原有 Markdown 与曾导致死循环的 Unicode 标题仍正常", () => {
  const html = render("## 标题 a\u2028b\n正文\n### 另一个\u2029标题\n\n1. 一\n2. 二\n\n> 引用\n\n**粗体**和*斜体*");
  assert.match(html, /标题 a\u2028b/);
  assert.match(html, /<ol/);
  assert.match(html, /<blockquote/);
  assert.match(html, /<strong/);
  assert.match(html, /<em/);
});

test("链接的目标地址保持原样，链接文字中的公式正常渲染", () => {
  const html = render(String.raw`[查看 $x$](https://example.com/$id$)

[引用][target]

[target]: https://example.com/$name$`);
  assert.equal(formulas(html), 1);
  assert.match(html, /href="https:\/\/example.com\/\$id\$"/);
  assert.match(html, /href="https:\/\/example.com\/\$name\$"/);
});
