"use client";

import { Fragment, useMemo, type ReactNode } from "react";

import { parseBlocks, type Block } from "@/lib/markdownBlocks";

/**
 * 极简 Markdown 渲染（模型实际会用的那几种写法）。
 *
 * 为什么自己写而不引第三方库：
 *   1. 这些文本是**模型产出、并且会引用论文/代码里的不可信内容**，
 *      渲染器越简单、攻击面越小。这里全程构造 React 节点，**从不使用 innerHTML**，
 *      所以原始 HTML/脚本在结构上就不可能被执行。
 *   2. 只需要粗体、斜体、行内代码、链接、列表、引用、代码块、小标题这几种，
 *      引一个完整解析器 + 插件链不划算。
 *
 * 支持：`**粗体**`、`*斜体*`、`` `代码` ``、`[文字](https://…)`、`- / 1.` 列表、
 * `> 引用`、``` 代码块 ```、`#`~`######` 标题。其余一律当纯文本。
 */

const INLINE = /`([^`]+)`|\*\*([^*]+)\*\*|\*([^*\n]+)\*|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;

/** 行内标记 → React 节点。没匹配上的部分原样当文本（React 会自动转义）。 */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let index = 0;
  INLINE.lastIndex = 0;
  let match = INLINE.exec(text);
  while (match !== null) {
    if (match.index > cursor) nodes.push(text.slice(cursor, match.index));
    const key = `${keyPrefix}-i${index++}`;
    if (match[1] !== undefined) {
      nodes.push(
        <code key={key} className="rounded bg-neutral-100 px-1 py-[1px] font-mono text-[0.92em] dark:bg-neutral-800">
          {match[1]}
        </code>,
      );
    } else if (match[2] !== undefined) {
      nodes.push(
        <strong key={key} className="font-semibold">
          {match[2]}
        </strong>,
      );
    } else if (match[3] !== undefined) {
      nodes.push(<em key={key}>{match[3]}</em>);
    } else {
      // 只允许 http(s) 链接（防 javascript:/data: 之类）
      nodes.push(
        <a key={key} href={match[5]} target="_blank" rel="noreferrer" className="underline">
          {match[4]}
        </a>,
      );
    }
    cursor = match.index + match[0].length;
    match = INLINE.exec(text);
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

/**
 * 行内模式：宿主容器本身不允许块级子元素时用（`<p>`、`<button>`）。
 *
 * 为什么必须有这个模式：块级模式的外层是个 `<div>`，塞进 `<p>` 里浏览器会在解析时
 * 提前把 `<p>` 闭合（DOM 与 React 想的不一样，浏览器控制台直接报
 * `<p> cannot contain a nested <div>`），塞进 `<button>` 里同样不合规范。
 * 行内模式只产出 span/strong/em/code/a 这类行内节点：段落之间用 `<br/>` 断行，
 * 列表项退化成「· 文本」前缀，绝不产生块级标签。
 */
function renderInlineBlocks(
  blocks: Block[],
  keyPrefix: string,
): ReactNode {
  const lines: string[] = [];
  blocks.forEach((block) => {
    switch (block.kind) {
      case "p":
        lines.push(block.lines.join(" "));
        break;
      case "h":
        lines.push(block.text);
        break;
      case "quote":
        lines.push(...block.lines);
        break;
      case "ul":
      case "ol":
        block.items.forEach((item, itemIndex) =>
          lines.push(block.kind === "ol" ? `${itemIndex + 1}. ${item}` : `· ${item}`),
        );
        break;
      case "code":
        lines.push(block.text);
        break;
    }
  });
  return lines.map((line, lineIndex) => (
    <Fragment key={`${keyPrefix}-l${lineIndex}`}>
      {lineIndex > 0 && <br />}
      {renderInline(line, `${keyPrefix}-l${lineIndex}`)}
    </Fragment>
  ));
}

export default function Markdown({
  text,
  className = "",
  inline = false,
}: {
  text: string;
  className?: string;
  inline?: boolean;
}) {
  const blocks = useMemo(() => parseBlocks(text ?? ""), [text]);
  if (inline) {
    return <span className={className}>{renderInlineBlocks(blocks, "in")}</span>;
  }
  return (
    <div className={`space-y-2 ${className}`}>
      {blocks.map((block, blockIndex) => {
        const key = `b${blockIndex}`;
        switch (block.kind) {
          case "h": {
            const Tag = (block.level <= 3 ? "h4" : "h5") as "h4" | "h5";
            return (
              <Tag key={key} className="font-semibold">
                {renderInline(block.text, key)}
              </Tag>
            );
          }
          case "code":
            return (
              <pre
                key={key}
                className="overflow-auto rounded bg-neutral-100 p-2 font-mono text-[12px] leading-relaxed dark:bg-neutral-900"
              >
                <code>{block.text}</code>
              </pre>
            );
          case "quote":
            return (
              <blockquote key={key} className="border-l-2 border-neutral-300 pl-2 text-neutral-600 dark:border-neutral-700 dark:text-neutral-400">
                {block.lines.map((line, lineIndex) => (
                  <Fragment key={`${key}-l${lineIndex}`}>
                    {lineIndex > 0 && <br />}
                    {renderInline(line, `${key}-l${lineIndex}`)}
                  </Fragment>
                ))}
              </blockquote>
            );
          case "ul":
            return (
              <ul key={key} className="md-list list-disc space-y-1 pl-5">
                {block.items.map((item, itemIndex) => (
                  <li key={`${key}-i${itemIndex}`}>{renderInline(item, `${key}-i${itemIndex}`)}</li>
                ))}
              </ul>
            );
          case "ol":
            return (
              <ol key={key} className="md-list list-decimal space-y-1 pl-5">
                {block.items.map((item, itemIndex) => (
                  <li key={`${key}-i${itemIndex}`}>{renderInline(item, `${key}-i${itemIndex}`)}</li>
                ))}
              </ol>
            );
          default:
            return (
              <p key={key} className="whitespace-pre-wrap">
                {renderInline(block.lines.join(" "), key)}
              </p>
            );
        }
      })}
    </div>
  );
}
