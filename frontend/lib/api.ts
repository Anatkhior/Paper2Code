import type { CreateRunResponse, ProviderConfig, SmokeResult } from "./types";

/** 后端地址。本地开发默认 8000；换端口就设 NEXT_PUBLIC_API_BASE。 */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

async function jsonOrThrow(response: Response) {
  if (!response.ok) {
    const text = await response.text();
    let detail = text.slice(0, 400);
    try {
      const parsed = JSON.parse(text);
      if (parsed?.detail) detail = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
    } catch {
      /* 保持原始文本 */
    }
    throw new Error(`HTTP ${response.status}：${detail}`);
  }
  return response.json();
}

export async function smokeTest(config: ProviderConfig): Promise<SmokeResult> {
  return jsonOrThrow(
    await fetch(`${API_BASE}/api/provider/smoke-test`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(config),
    }),
  );
}

export async function createRun(file: File, config: ProviderConfig): Promise<CreateRunResponse> {
  const form = new FormData();
  form.append("file", file);
  // api_key 只放在这一次请求的 body 里：后端不落库、不写日志，前端也不持久化
  form.append("provider", JSON.stringify(config));
  return jsonOrThrow(await fetch(`${API_BASE}/api/runs`, { method: "POST", body: form }));
}

export async function startRecon(runId: string, config: ProviderConfig) {
  return jsonOrThrow(
    await fetch(`${API_BASE}/api/runs/${runId}/recon`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ provider: config }),
    }),
  );
}

export async function fetchPlan(runId: string) {
  return jsonOrThrow(await fetch(`${API_BASE}/api/runs/${runId}/plan`));
}

/** 把"我想看懂这一段"变成一个定位目标（在原文里划选后调用）。 */
export async function addPlanItem(
  runId: string,
  payload: { page?: number; quote?: string; name?: string; search_hints?: string[]; one_liner?: string },
) {
  return jsonOrThrow(
    await fetch(`${API_BASE}/api/runs/${runId}/plan/items`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function patchPlanItem(
  runId: string,
  itemId: string,
  patch: { name?: string; one_liner?: string; search_hints?: string[] },
) {
  return jsonOrThrow(
    await fetch(`${API_BASE}/api/runs/${runId}/plan/items/${itemId}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function deletePlanItem(runId: string, itemId: string) {
  return jsonOrThrow(
    await fetch(`${API_BASE}/api/runs/${runId}/plan/items/${itemId}`, { method: "DELETE" }),
  );
}

export async function fetchChat(runId: string) {
  return jsonOrThrow(await fetch(`${API_BASE}/api/runs/${runId}/chat`));
}

/** 追问一句。Agent 会自己决定要不要去翻论文/代码。 */
export async function postChat(
  runId: string,
  payload: { provider: ProviderConfig; message: string; context_ids?: string[] },
) {
  return jsonOrThrow(
    await fetch(`${API_BASE}/api/runs/${runId}/chat`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

export async function startLocate(
  runId: string,
  config: ProviderConfig,
  repoUrl: string,
  selectedIds: string[],
) {
  return jsonOrThrow(
    await fetch(`${API_BASE}/api/runs/${runId}/locate`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ provider: config, repo_url: repoUrl, selected_ids: selectedIds }),
    }),
  );
}

export async function cancelRun(runId: string) {
  return jsonOrThrow(await fetch(`${API_BASE}/api/runs/${runId}/cancel`, { method: "POST" }));
}

export interface FileView {
  path: string;
  commit_sha: string;
  line_start: number;
  line_end: number;
  total_lines: number;
  truncated: boolean;
  lines: { n: number; text: string }[];
  focus: { start: number; end: number };
  source_url: string | null;
}

export interface PaperPageView {
  page: number;
  page_count: number;
  title_guess: string;
  text: string;
}

/** 读某个 commit 上的一段代码。后端的核验也是读这份内容，所以两边必然一致。 */
export async function fetchFile(
  runId: string,
  path: string,
  start: number,
  end: number,
): Promise<FileView> {
  const query = new URLSearchParams({
    path,
    start: String(start),
    end: String(end),
    focus_start: String(start),
    focus_end: String(end),
  });
  return jsonOrThrow(await fetch(`${API_BASE}/api/runs/${runId}/file?${query.toString()}`));
}

/** 论文 PDF 的直链，交给浏览器自带的阅读器渲染（真实排版） */
export function pdfUrl(runId: string) {
  return `${API_BASE}/api/runs/${runId}/pdf`;
}

export async function fetchPaperPage(runId: string, page: number): Promise<PaperPageView> {
  return jsonOrThrow(await fetch(`${API_BASE}/api/runs/${runId}/paper/page/${page}`));
}

/**
 * SSE 地址。fromId 是"我已经收到的最大事件 id"。
 *
 * 为什么要带它：浏览器新建 EventSource 时带不上 Last-Event-ID 头，
 * 而后端会**重放历史**。第二个阶段重开流时，历史里那个 run_end 会让前端以为整件事已经结束了。
 */
export function eventsUrl(runId: string, fromId?: number) {
  const base = `${API_BASE}/api/runs/${runId}/events`;
  return fromId && fromId > 0 ? `${base}?from_id=${fromId}` : base;
}

export function healthUrl() {
  return `${API_BASE}/api/health`;
}
