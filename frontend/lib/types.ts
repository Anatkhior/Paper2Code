/** 与后端事件契约一一对应的类型（docs/v0-spec.md §9）。 */

export type Protocol = "openai-compatible" | "anthropic";

export interface ProviderConfig {
  protocol: Protocol;
  base_url?: string | null;
  api_key: string;
  model: string;
  label?: string | null;
}

export interface PaperMeta {
  title_guess: string;
  page_count: number;
  sha256: string;
  bytes: number;
  filename?: string | null;
}

export interface PaperEvidence {
  page: number;
  quote: string;
  kind?: "text" | "equation" | "figure" | "table";
  /** 后端逐条核验的结果：这句话真的在那一页吗？null = 无法核验（例如无论文上下文） */
  verified?: boolean | null;
  /**
   * 匹配分级：full=逐字出现在那一页；partial=只有开头 60 字符匹配
   * （后端容忍轻微抄错，但必须如实标出来——"真开头+编造后半段"不能冒充逐字引用）。
   */
  quote_match?: "full" | "partial" | null;
}

export interface Innovation {
  id: string;
  /** "user" 表示这条是用户自己在原文里划选加进来的，不是 Agent 梳理出来的 */
  source?: "user" | "agent";
  name: string;
  one_liner: string;
  difficulty: "beginner" | "medium" | "hard";
  paper_evidence: PaperEvidence[];
  search_hints: string[];
}

export interface Plan {
  paper_summary: string;
  coverage_note?: string;
  innovations: Innovation[];
}

export interface CodeEvidence {
  path: string;
  line_start: number;
  line_end: number;
  symbol?: string | null;
  why: string;
  quote?: string | null;
  /** 后端读出来自己算的片段哈希 —— 模型无权提供，用来防"事后改代码" */
  snippet_sha256?: string | null;
  verification?: {
    state: "verified" | "failed" | "pending";
    checked_at: string | null;
    failures: string[];
    file_lines?: number | null;
  };
}

export interface Finding {
  id: string;
  name: string | null;
  one_liner: string | null;
  difficulty: string | null;
  paper_evidence: PaperEvidence[];
  search_hints: string[];
  status: "matched" | "partial" | "not_found";
  confidence: number;
  confidence_reason: string;
  code_evidence: CodeEvidence[];
  explanation: {
    intuition?: string;
    math?: string;
    code_walkthrough?: { line_ref: string; text: string }[];
    /** 容易搞错的地方（后端要求至少一条，否则会把这次提交打回） */
    pitfalls?: string[];
    read_next?: string[];
  };
  not_found_reason?: string | null;
  searched: string[];
}

export interface VerificationSummary {
  innovations: number;
  status_counts: Record<string, number>;
  commit_sha: string;
  citations_total: number;
  citations_verified: number;
  citations_failed: number;
  citation_verifiable_rate: number;
  failures: { innovation_id: string; path: string; line_start: number; failures: string[] }[];
}

export interface SmokeStep {
  turn: number;
  text: string;
  tool_calls: { name: string; arguments: Record<string, unknown> }[];
  finish_reason: string | null;
  latency_ms: number;
}

export interface ProbeResult {
  url: string;
  status?: number | null;
  content_type?: string;
  snippet?: string;
  is_html?: boolean;
  error?: string;
}

export interface SmokeResult {
  ok: boolean;
  diagnosis: string;
  capabilities: Record<string, unknown>;
  steps: SmokeStep[];
  provider: string;
  /** 自检失败时顺手探测端点的结果（用来分清"网关坏了"和"地址写错了"） */
  diagnostics?: { probes?: ProbeResult[]; note?: string };
}

export interface RunEvent {
  id: number;
  type: string;
  ts: number;
  data: Record<string, any>;
}

export interface CreateRunResponse {
  run_id: string;
  paper: PaperMeta;
  provider: string;
  events_url: string;
}

export interface BudgetLimits {
  max_tool_calls: number;
  max_input_tokens: number;
  wall_clock_seconds: number;
  max_upload_mb?: number;
  max_pages?: number;
}

/**
 * 后端发的是具名 SSE 事件（`event: tool_call`），
 * 而浏览器的 EventSource 只有在事件没有名字时才会触发 onmessage。
 * 所以必须按类型逐个 addEventListener —— 漏掉一个类型，前端就会静默收不到它。
 */
export const EVENT_TYPES = [
  "paper_ready",
  "run_start",
  "step_start",
  "assistant_text",
  "tool_call",
  "tool_result",
  "plan_ready",
  "plan_updated",
  "repo_cloning",
  "clone_progress",
  "repo_ready",
  "finding",
  "verification_done",
  "chat_user",
  "chat_reply",
  "budget_warning",
  "error",
  "run_end",
] as const;
