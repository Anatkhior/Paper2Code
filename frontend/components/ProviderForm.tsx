"use client";

import type { ProviderConfig, SmokeResult } from "@/lib/types";

interface Props {
  value: ProviderConfig;
  onChange: (next: ProviderConfig) => void;
  smoke: SmokeResult | null;
  smokeBusy: boolean;
  onSmoke: () => void;
  onSave: () => void;
}

const PRESETS: { label: string; config: Partial<ProviderConfig> }[] = [
  { label: "OpenAI", config: { protocol: "openai-compatible", base_url: "https://api.openai.com/v1", model: "gpt-4o" } },
  { label: "DeepSeek", config: { protocol: "openai-compatible", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" } },
  { label: "Anthropic", config: { protocol: "anthropic", base_url: "https://api.anthropic.com", model: "claude-sonnet-4-6" } },
  { label: "本地 Ollama", config: { protocol: "openai-compatible", base_url: "http://127.0.0.1:11434/v1", model: "qwen2.5:14b" } },
];

export default function ProviderForm({ value, onChange, smoke, smokeBusy, onSmoke, onSave }: Props) {
  const set = (patch: Partial<ProviderConfig>) => onChange({ ...value, ...patch });

  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-950">
      <header className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold">1. 模型（自带 key）</h2>
        <div className="flex gap-1">
          {PRESETS.map((preset) => (
            <button
              key={preset.label}
              type="button"
              onClick={() => set(preset.config)}
              className="rounded border border-neutral-300 px-2 py-0.5 text-xs text-neutral-600 hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800"
            >
              {preset.label}
            </button>
          ))}
        </div>
      </header>

      <div className="grid grid-cols-2 gap-2 text-sm">
        <label className="col-span-2 flex flex-col gap-1">
          <span className="text-xs text-neutral-500">协议</span>
          <select
            value={value.protocol}
            onChange={(event) => set({ protocol: event.target.value as ProviderConfig["protocol"] })}
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 dark:border-neutral-700"
          >
            <option value="openai-compatible">OpenAI 兼容（OpenAI / DeepSeek / Moonshot / Groq / vLLM / Ollama…）</option>
            <option value="anthropic">Anthropic</option>
          </select>
        </label>

        <label className="col-span-2 flex flex-col gap-1">
          <span className="text-xs text-neutral-500">base_url（官方端点可留空）</span>
          <input
            value={value.base_url ?? ""}
            onChange={(event) => set({ base_url: event.target.value })}
            placeholder="https://api.deepseek.com/v1"
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-neutral-700"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs text-neutral-500">api_key（只放在请求体里，不落库、不进日志）</span>
          <input
            type="password"
            value={value.api_key}
            onChange={(event) => set({ api_key: event.target.value })}
            placeholder="sk-..."
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-neutral-700"
          />
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs text-neutral-500">模型名</span>
          <input
            value={value.model}
            onChange={(event) => set({ model: event.target.value })}
            placeholder="deepseek-chat"
            className="rounded border border-neutral-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-neutral-700"
          />
        </label>
      </div>

      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          onClick={onSmoke}
          disabled={smokeBusy}
          className="rounded bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40 dark:bg-neutral-100 dark:text-neutral-900"
        >
          {smokeBusy ? "自检中…" : "运行自检（强烈建议）"}
        </button>
        <button
          type="button"
          onClick={onSave}
          disabled={!value.api_key || !value.model}
          className="rounded border border-neutral-300 px-3 py-1.5 text-xs disabled:opacity-40 dark:border-neutral-700"
        >
          存到本机浏览器
        </button>
        {smoke && (
          <span className={`text-xs ${smoke.ok ? "text-green-600" : "text-red-600"}`}>
            {smoke.ok ? "✅ 通过" : "❌ 不通过"}
          </span>
        )}
      </div>

      {/* 通过时只留一句人话：capabilities 那种 JSON 是给排障用的，成功时摆出来只会挤占视线。
          不通过才把技术细节（capabilities / 端点探测 / 每轮工具调用）全摊开——那时候它们有用。 */}
      {smoke?.ok && (
        <div className="smoke-compact mt-3 rounded border border-green-300 bg-green-50 p-2 text-xs text-green-900 dark:border-green-900 dark:bg-green-950 dark:text-green-100">
          <p className="font-medium">{smoke.diagnosis}</p>
          <p className="mt-1 text-[11px] opacity-80">{smoke.provider}</p>
        </div>
      )}

      {smoke && !smoke.ok && (
        <div className="mt-3 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-100">
          <p className="font-medium">{smoke.diagnosis}</p>
          <p className="mt-1 font-mono text-[11px] opacity-80">
            {smoke.provider} · capabilities {JSON.stringify(smoke.capabilities)}
          </p>
          {smoke.diagnostics?.probes?.length ? (
            <div className="mt-2 space-y-1 rounded border border-neutral-300 bg-white/60 p-2 dark:border-neutral-700 dark:bg-neutral-900/60">
              <p className="font-medium">端点探测（直接 GET 它的 /models）</p>
              {smoke.diagnostics.probes.map((probe) => (
                <p key={probe.url} className="break-all font-mono text-[10px]">
                  {probe.url} → {probe.status ?? probe.error}
                  {probe.is_html ? "（返回的是 HTML，不是 JSON）" : ""}
                  {probe.snippet && !probe.is_html ? ` · ${probe.snippet.slice(0, 80)}` : ""}
                </p>
              ))}
              <p className="pt-1 text-[10px]">你也可以自己敲一遍，看它到底回什么：</p>
              <code className="block break-all rounded bg-neutral-100 p-1 font-mono text-[10px] dark:bg-neutral-800">
                curl -sS -o /dev/null -w &apos;%{"{http_code}"}\n&apos; {value.base_url || "https://api.example.com/v1"}/models -H
                &quot;Authorization: Bearer $KEY&quot;
              </code>
            </div>
          ) : null}
          {smoke.steps.map((step) => (
            <p key={step.turn} className="mt-1 font-mono text-[11px] opacity-80">
              第 {step.turn} 轮：{step.tool_calls.map((call) => `${call.name}(${JSON.stringify(call.arguments)})`).join(", ") || step.text.slice(0, 60)}
              {" · "}
              {step.latency_ms}ms
            </p>
          ))}
        </div>
      )}

      {(!value.api_key || !value.model) && (
        <p className="mt-2 text-[11px] text-amber-600">还差：{!value.api_key ? "api_key" : "模型名"}</p>
      )}
      <p className="mt-2 text-[11px] leading-relaxed text-neutral-500">
        自检会跑两轮工具调用。**本项目完全依赖可靠的 function calling**：不支持工具调用、或参数乱填的模型，
        会在这里被拦住并告诉你原因，而不是产出一堆看起来像模像样的垃圾。
      </p>
    </section>
  );
}
