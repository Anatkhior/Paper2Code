import type { OutputLanguage } from "./types";

/** 解析语言独立于模型设置；只影响新生成的内容，不翻译证据原文。 */
export const OUTPUT_LANGUAGE_STORAGE_KEY = "paper2code.outputLanguage";
export const DEFAULT_OUTPUT_LANGUAGE: OutputLanguage = "zh-CN";

export function normalizeOutputLanguage(value: unknown): OutputLanguage {
  return value === "en" ? "en" : DEFAULT_OUTPUT_LANGUAGE;
}
