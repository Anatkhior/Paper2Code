"use client";

import { memo, useMemo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { normalizeMath, displayMath } from "@/lib/markdownMath";

const heading = ({ children }: { children?: ReactNode }) => <h4 className="font-semibold">{children}</h4>;
const inlineBlock = ({ children }: { children?: ReactNode }) => <span>{children}<br /></span>;

const components: Components = {
  h1: heading, h2: heading, h3: heading, h4: heading, h5: heading, h6: heading,
  p: ({ children }) => <p className="whitespace-pre-wrap">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noreferrer" className="underline">{children}</a> : <>{children}</>,
  // Keep model HTML and images inert, as in the original text-only renderer.
  img: ({ alt }) => <span>{alt}</span>,
  code: ({ children }) => <code className="md-code rounded bg-neutral-100 px-1 py-px font-mono text-[0.92em] dark:bg-neutral-800">{children}</code>,
  pre: ({ children }) => <pre className="overflow-auto rounded bg-neutral-100 p-2 font-mono text-xs leading-relaxed dark:bg-neutral-900">{children}</pre>,
  blockquote: ({ children }) => <blockquote className="border-l-2 border-neutral-300 pl-2 text-neutral-600 dark:border-neutral-700 dark:text-neutral-400">{children}</blockquote>,
  ul: ({ children }) => <ul className="md-list list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children, start }) => <ol start={start} className="md-list list-decimal space-y-1 pl-5">{children}</ol>,
};

// Titles and button labels must contain only phrasing content, including math.
const inlineComponents: Components = {
  ...components,
  h1: inlineBlock, h2: inlineBlock, h3: inlineBlock, h4: inlineBlock, h5: inlineBlock, h6: inlineBlock,
  p: inlineBlock, blockquote: inlineBlock, pre: inlineBlock,
  ul: ({ children }) => <span>{children}</span>,
  ol: ({ children }) => <span>{children}</span>,
  li: ({ children }) => <span>· {children}<br /></span>,
  hr: () => <br />,
  a: ({ children }) => <span className="underline">{children}</span>,
};

export default memo(function Markdown({ text, className = "", inline = false, math = false }: {
  text: string;
  className?: string;
  inline?: boolean;
  /** The structured explanation.math field may contain bare TeX. */
  math?: boolean;
}) {
  const source = useMemo(() => normalizeMath(math ? displayMath(text) : text), [text, math]);
  const Wrapper = inline ? "span" : "div";
  return <Wrapper className={`markdown ${inline ? "markdown-inline" : "space-y-2"} ${className}`}>
    <ReactMarkdown
      remarkPlugins={[[remarkMath, { singleDollarTextMath: false }]]}
      rehypePlugins={[[rehypeKatex, {
        trust: false,
        // Chinese operator/text names are intentional, not malformed TeX.
        strict: (code: string) => code === "unicodeTextInMathMode" ? "ignore" : "warn",
      }]]}
      urlTransform={(url) => /^https?:\/\//i.test(url) ? url : ""}
      components={inline ? inlineComponents : components}
    >{source}</ReactMarkdown>
  </Wrapper>;
});
