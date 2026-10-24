import type { RunEvent, RunSnapshot } from "./types";

export function appendEvent(snapshot: RunSnapshot, event: RunEvent): RunSnapshot {
  if (event.id <= snapshot.event_cursor) return snapshot;
  const next = { ...snapshot, event_cursor: event.id, events: [...snapshot.events, event] };
  const data = event.data;
  if (event.type === "paper_ready") next.paper = data.paper;
  if (event.type === "plan_ready" || event.type === "plan_updated") next.plan = data.plan;
  if (event.type === "repo_ready") next.repo = data.repo;
  if (event.type === "finding") next.findings = [...next.findings.filter(item => item.id !== data.finding.id), data.finding];
  if (event.type === "verification_done") next.verification = data.summary;
  if (event.type === "run_start") {
    next.active = true;
    next.history = { ...snapshot.history, latest_operation: {
      id: event.operation_id ?? undefined, phase: data.meta?.phase, status: "running",
      provider: { model: data.model, protocol: data.provider },
    } };
  }
  if (event.type === "run_end") {
    next.active = false;
    if (snapshot.history?.latest_operation) next.history = { ...snapshot.history,
      latest_operation: { ...snapshot.history.latest_operation, status: data.status, reason: data.stopped_reason } };
  }
  return next;
}

export const HISTORY_STATUS: Record<string, string> = {
  uploaded: "已上传", running: "进行中", recon_complete: "已梳理创新点", complete: "解读完成",
  partial: "部分完成", failed: "失败，已保存进展", cancelled: "已取消", interrupted: "任务已中断", unreadable: "记录需要修复",
};

export function providerChange(previous: import("./types").ProviderConfig, next: import("./types").ProviderConfig) {
  const endpoint = (value?: string | null) => (value ?? "").trim().replace(/\/+$/, "");
  const changed = previous.protocol !== next.protocol || endpoint(previous.base_url) !== endpoint(next.base_url);
  return changed ? { ...next, api_key: "" } : next;
}
