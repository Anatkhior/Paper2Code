/** Agent 工具的中文名：轨迹与追问共用，未知工具回退为原名。 */
export const TOOL_LABELS: Record<string, string> = {
  list_pages: "列页",
  get_page_text: "读论文页",
  search_paper: "搜论文",
  read_paper_all: "读全文",
  repo_tree: "看目录",
  search_code: "搜代码",
  read_file: "读代码",
  record_plan: "记录清单",
  record_finding: "记录结论",
  read_chat_history: "读历史对话",
  finish: "结束",
  ping: "自检",
};

export const toolLabel = (tool: unknown) => TOOL_LABELS[String(tool)] ?? String(tool);

/** 把工具参数压成一句可读摘要；完整 JSON 仍放在详情里。 */
export function toolArgsSummary(args: unknown): string {
  if (!args || typeof args !== "object") return "";
  const value = args as Record<string, unknown>;
  if (typeof value.path === "string") {
    const range = typeof value.start_line === "number" ? `:${value.start_line}${typeof value.end_line === "number" ? `-${value.end_line}` : ""}` : "";
    return `${value.path}${range}`;
  }
  if (typeof value.page === "number") return `第 ${value.page} 页`;
  const query = value.query ?? value.pattern;
  if (typeof query === "string" && query) return `“${query.length > 48 ? `${query.slice(0, 48)}…` : query}”`;
  return "";
}
