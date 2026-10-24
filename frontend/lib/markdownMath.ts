import { fromMarkdown } from "mdast-util-from-markdown";
import type { Root, RootContent } from "mdast";

/** Normalize model math delimiters before Markdown can consume backslashes.
 * Code and escaped delimiters stay literal; unmatched streaming input stays text.
 */
function closingDelimiter(text: string, start: number, delimiter: string): number {
  let braces = 0;
  for (let i = start; i < text.length; i++) {
    if (!braces && text.startsWith(delimiter, i)) {
      return delimiter !== "$" || (!/\s/.test(text[i - 1]) && !/\d/.test(text[i + 1] ?? "")) ? i : -1;
    }
    if (text[i] === "\\") i++;
    else if (text[i] === "{") braces++;
    else if (text[i] === "}") braces = Math.max(0, braces - 1);
  }
  return -1;
}

export function displayMath(text: string): string {
  const trimmed = text.trim();
  return /^(\\\[|\\\(|\$)/.test(trimmed) ? trimmed : `\\[${trimmed}\\]`;
}

export function normalizeMath(text: string): string {
  const source = text.replace(/\r\n?/g, "\n");
  // Use CommonMark positions so code and link destinations retain their bytes.
  const literalRanges: { start: number; end: number }[] = [];
  function collectLiterals(node: Root | RootContent) {
    if (node.position) {
      const start = node.position.start.offset!;
      const end = node.position.end.offset!;
      if (["code", "inlineCode", "definition", "html", "image"].includes(node.type)) {
        literalRanges.push({ start, end });
        return;
      }
      if (node.type === "link") {
        const destination = source.slice(start, end).lastIndexOf("](");
        literalRanges.push({ start: destination < 0 ? start : start + destination + 2, end });
      }
    }
    if ("children" in node) node.children.forEach(collectLiterals);
  }
  collectLiterals(fromMarkdown(source));
  literalRanges.sort((a, b) => a.start - b.start);
  let literalIndex = 0;
  let result = "";
  let i = 0;
  while (i < source.length) {
    while (literalRanges[literalIndex]?.end <= i) literalIndex++;
    const literal = literalRanges[literalIndex];
    if (literal?.start === i) {
      result += source.slice(i, literal.end);
      i = literal.end;
      continue;
    }
    const opener = ["\\[", "\\(", "$$", "$"].find((delimiter) => source.startsWith(delimiter, i));
    if (opener && (opener !== "$" || !/\s/.test(source[i + 1] ?? " "))) {
      const closer = opener === "\\[" ? "\\]" : opener === "\\(" ? "\\)" : opener;
      const end = closingDelimiter(source, i + opener.length, closer);
      if (end !== -1) {
        const formula = source.slice(i + opener.length, end);
        const display = opener === "\\[" || opener === "$$";
        if (display) {
          // Keep standalone formulas inside their list/blockquote indentation.
          const before = source.slice(source.lastIndexOf("\n", i - 1) + 1, i);
          const prefix = /^[ \t>]*$/.test(before) ? before : "";
          const body = prefix ? formula.replaceAll(`\n${prefix}`, "\n") : formula;
          result += `${prefix === before ? "" : "\n\n"}$$\n${body.trim().split("\n").map((line) => prefix + line).join("\n")}\n${prefix}$$\n${prefix}`;
        } else {
          // Single dollars are recognized here with currency-aware boundaries.
          result += `$$${formula}$$`;
        }
        i = end + closer.length;
        continue;
      }
      // Prevent an unfinished $$ block from consuming the rest as valid math.
      if (opener === "$$") {
        result += "\\$\\$";
        i += 2;
        continue;
      }
    }
    if (source[i] === "\\" && i + 1 < source.length) {
      result += source.slice(i, i + 2);
      i += 2;
    } else {
      result += source[i++];
    }
  }
  return result;
}
