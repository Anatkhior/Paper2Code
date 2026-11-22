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

const endpoint = (value?: string | null) => (value ?? "").trim().replace(/\/+$/, "");

export default function ProviderForm({ value, onChange, smoke, smokeBusy, onSmoke, onSave }: Props) {
  const set = (patch: Partial<ProviderConfig>) => onChange({ ...value, ...patch });
  const isActive = (preset: Partial<ProviderConfig>) =>
    preset.protocol === value.protocol && endpoint(preset.base_url) === endpoint(value.base_url) && preset.model === value.model;

  return (
    <section>
      <header className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">模型设置（自带 key）</h2>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="常用来源">
          {PRESETS.map((preset) => (
            <button key={preset.label} type="button" aria-pressed={isActive(preset.config)} onClick={() => set(preset.config)} className="chip">
              {preset.label}
            </button>
          ))}
        </div>
      </header>

      <div className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
        <label className="flex min-w-0 flex-col gap-1.5 sm:col-span-2">
          <span className="text-xs text-muted">协议</span>
          <select
            value={value.protocol}
            onChange={(event) => set({ protocol: event.target.value as ProviderConfig["protocol"] })}
            className="field"
          >
            <option value="openai-compatible">OpenAI 兼容（OpenAI / DeepSeek / Moonshot / Groq / vLLM / Ollama…）</option>
            <option value="anthropic">Anthropic</option>
          </select>
        </label>

        <label className="flex min-w-0 flex-col gap-1.5 sm:col-span-2">
          <span className="text-xs text-muted">base_url（官方端点可留空）</span>
          <input
            value={value.base_url ?? ""}
            onChange={(event) => set({ base_url: event.target.value })}
            placeholder="https://api.deepseek.com/v1"
            spellCheck={false}
            className="field font-mono"
          />
        </label>

        <label className="flex min-w-0 flex-col gap-1.5">
          <span className="text-xs text-muted">api_key（只放在请求体里，不落库、不进日志）</span>
          <input
            type="password"
            value={value.api_key}
            onChange={(event) => set({ api_key: event.target.value })}
            placeholder="sk-..."
            autoComplete="off"
            className="field font-mono"
          />
        </label>

        <label className="flex min-w-0 flex-col gap-1.5">
          <span className="text-xs text-muted">模型名</span>
          <input
            value={value.model}
            onChange={(event) => set({ model: event.target.value })}
            placeholder="deepseek-chat"
            spellCheck={false}
            className="field font-mono"
          />
        </label>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button type="button" onClick={onSmoke} disabled={smokeBusy} className="secondary-button">
          {smokeBusy ? "自检中…" : "运行自检（强烈建议）"}
        </button>
        <button type="button" onClick={onSave} className="quiet-button min-h-10">
          存到本机浏览器
        </button>
        {smoke && <span className="badge" data-tone={smoke.ok ? "ok" : "fail"}>{smoke.ok ? "自检通过" : "自检未通过"}</span>}
      </div>

      {/* 通过时只留一句人话：capabilities 那种 JSON 是给排障用的，成功时摆出来只会挤占视线。
          不通过才把技术细节（capabilities / 端点探测 / 每轮工具调用）全摊开——那时候它们有用。 */}
      {smoke?.ok && (
        <div className="smoke-compact alert-ok mt-3 rounded-lg px-3 py-2 text-xs">
          <p className="font-medium">{smoke.diagnosis}</p>
          <p className="mt-1 text-[11px] opacity-80">{smoke.provider}</p>
        </div>
      )}

      {smoke && !smoke.ok && (
        <div className="alert-fail mt-3 rounded-lg px-3 py-2 text-xs">
          <p className="font-medium">{smoke.diagnosis}</p>
          <p className="mt-1 break-all font-mono text-[11px] opacity-80">
            {smoke.provider} · capabilities {JSON.stringify(smoke.capabilities)}
          </p>
          {smoke.diagnostics?.probes?.length ? (
            <div className="mt-2 space-y-1 rounded-md border border-line bg-surface/80 p-2">
              <p className="font-medium">端点探测（直接 GET 它的 /models）</p>
              {smoke.diagnostics.probes.map((probe) => (
                <p key={probe.url} className="break-all font-mono text-[11px]">
                  {probe.url} → {probe.status ?? probe.error}
                  {probe.is_html ? "（返回的是 HTML，不是 JSON）" : ""}
                  {probe.snippet && !probe.is_html ? ` · ${probe.snippet.slice(0, 80)}` : ""}
                </p>
              ))}
              <p className="pt-1 text-[11px]">你也可以自己敲一遍，看它到底回什么：</p>
              <code className="block break-all rounded bg-subtle p-1.5 font-mono text-[11px]">
                curl -sS -o /dev/null -w &apos;%{"{http_code}"}\n&apos; {value.base_url || "https://api.example.com/v1"}/models -H
                &quot;Authorization: Bearer $KEY&quot;
              </code>
            </div>
          ) : null}
          {smoke.steps.map((step) => (
            <p key={step.turn} className="mt-1 break-all font-mono text-[11px] opacity-80">
              第 {step.turn} 轮：{step.tool_calls.map((call) => `${call.name}(${JSON.stringify(call.arguments)})`).join(", ") || step.text.slice(0, 60)}
              {" · "}
              {step.latency_ms}ms
            </p>
          ))}
        </div>
      )}

      {(!value.api_key || !value.model) && (
        <p className="mt-3 text-xs text-warn">还差：{[!value.api_key && "api_key", !value.model && "模型名"].filter(Boolean).join("、")}</p>
      )}
      <p className="mt-2 text-xs leading-5 text-muted">
        自检会跑两轮工具调用。<strong className="font-semibold text-foreground">本项目完全依赖可靠的 function calling</strong>：不支持工具调用、或参数乱填的模型，会在这里被拦住并告诉你原因，而不是产出一堆看起来像模像样的垃圾。
      </p>
    </section>
  );
}
