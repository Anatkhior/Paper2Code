import Prism from "prismjs";
import "prismjs/components/prism-python.js";
import "prismjs/components/prism-typescript.js";
import "prismjs/components/prism-jsx.js";
import "prismjs/components/prism-tsx.js";
import "prismjs/components/prism-c.js";
import "prismjs/components/prism-cpp.js";
import "prismjs/components/prism-json.js";
import "prismjs/components/prism-bash.js";
import "prismjs/components/prism-yaml.js";
import "prismjs/components/prism-rust.js";
import "prismjs/components/prism-go.js";
import "prismjs/components/prism-java.js";

const LANGUAGES: Record<string, string> = {
  py: "python", pyi: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", jsx: "jsx", tsx: "tsx", c: "c", h: "c", cpp: "cpp", cc: "cpp",
  hpp: "cpp", cu: "cpp", cuh: "cpp", json: "json", sh: "bash", bash: "bash",
  yaml: "yaml", yml: "yaml", rs: "rust", go: "go", java: "java", css: "css",
  html: "markup", xml: "markup", svg: "markup",
};

export interface CodeSegment { text: string; types: string[] }

/** 先解析整个文件再分行，保留多行注释/字符串的语法上下文。只返回文本，交给 React 转义。 */
export function highlightCode(path: string, source: string) {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  const language = LANGUAGES[extension];
  const tokens = language ? Prism.tokenize(source, Prism.languages[language]) : [source];
  const lines: CodeSegment[][] = [[]];
  const append = (value: string | Prism.Token | (string | Prism.Token)[], types: string[] = []) => {
    if (Array.isArray(value)) { value.forEach((part) => append(part, types)); return; }
    if (typeof value !== "string") {
      const aliases = typeof value.alias === "string" ? [value.alias] : value.alias ?? [];
      append(value.content, [...types, value.type, ...aliases]);
      return;
    }
    value.split("\n").forEach((text, index) => {
      if (index) lines.push([]);
      if (text) lines[lines.length - 1].push({ text, types });
    });
  };
  append(tokens);
  return { language: language ?? "纯文本", lines };
}
