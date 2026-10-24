import { expect, test } from "@playwright/test";
import { createServer, type ServerResponse } from "node:http";
import type { RunEvent, RunSnapshot } from "../../lib/types";

const answer = String.raw`状态更新分为四步：

\[x=\operatorname{拼接}\bigl(\phi(S),a,\xi_n\bigr)\]

\[y=\operatorname{MLP}\bigl(\operatorname{输入归一化}(x)\bigr)\]

\[d=y\odot\sigma_{\mathrm{out}}+\mu_{\mathrm{out}}\]

\[\hat S_{t+1}=\operatorname{状态更新}(S_t,d)\]

其中 $x$ 是输入。`;

function snapshot(): RunSnapshot {
  const finding = {
    id: "math", name: "状态更新", one_liner: null, difficulty: null, search_hints: [],
    paper_evidence: [], status: "not_found" as const, confidence: 0, confidence_reason: "测试记录",
    code_evidence: [], explanation: { math: String.raw`W=W_0+\frac{\alpha}{r}BA` }, searched: [],
  };
  return {
    run_id: "math-preview", paper: null, repo: null,
    plan: { paper_summary: "数学排版测试", innovations: [{ id: "math", name: "状态更新", one_liner: "用于渲染验证", difficulty: "beginner", paper_evidence: [], search_hints: [] }] },
    artifact: { innovations: [finding] }, findings: [finding], verification: null,
    turns: [{ role: "assistant", text: answer, status: "ok" }],
    history: { run_id: "math-preview", title: "公式测试", created_at: 1, updated_at: 1, analysis_status: "complete", innovation_count: 1, result_count: 1, warnings: [] },
    resources: { paper: false, repo: false }, warnings: [], can_chat: true, active: false,
    events: [], event_cursor: 0, has_older_events: false,
  };
}

test("历史与裸公式渲染，中文数学字体加载，长公式只在自身滚动", async ({ page }, info) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const value = snapshot();
  value.turns[0].text += '\n\n```latex\n\\[x\\]\n```\n\n' + String.raw`\[L=${Array.from({ length: 24 }, (_, i) => `x_{${i}}`).join("+")}\]`;
  await page.route("**/api/runs/math-preview/snapshot", route => route.fulfill({ json: value }));
  await page.goto("/?run=math-preview");
  const chat = page.locator("section").filter({ has: page.getByRole("heading", { name: "追问", exact: true }) }).last();
  await expect(chat.locator(".katex")).toHaveCount(6);
  await expect(chat.locator(".katex-error")).toHaveCount(0);
  await expect(chat.locator("pre code")).toHaveText(String.raw`\[x\]` + "\n");
  await page.getByText("公式 · 模型重构，非论文原文", { exact: true }).click();
  await expect(page.locator(".finding-detail .katex")).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  const font = await chat.locator(".katex .mop").first().evaluate(element => getComputedStyle(element).fontFamily);
  expect(font).toContain("KaTeX");
  if (process.env.PAPER2CODE_CAPTURE_SCREENSHOTS) await chat.screenshot({ path: info.outputPath("math-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const longFormula = chat.locator(".katex-display").last();
  expect(await longFormula.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
  await page.emulateMedia({ colorScheme: "dark" });
  if (process.env.PAPER2CODE_CAPTURE_SCREENSHOTS) await chat.screenshot({ path: info.outputPath("math-mobile-dark.png") });
  await page.reload();
  await expect(chat.locator(".katex")).toHaveCount(6);
  expect(errors).toEqual([]);
});

test("流式公式闭合时自动渲染，完成及刷新后仍只有一份回答", async ({ page }) => {
  let stream: ServerResponse | undefined;
  const server = createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/event-stream", "Access-Control-Allow-Origin": "*" });
    response.write(": connected\n\n");
    stream = response;
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing test server address");
  const event = (id: number, type: string, data: RunEvent["data"]): RunEvent => ({ id, type, data, ts: id, operation_id: "math-chat" });
  const value = snapshot();
  value.active = true;
  value.turns = [];
  value.history.latest_operation = { id: "math-chat", phase: "chat", status: "running" };
  value.events = [event(1, "chat_user", { message: "解释公式" }), event(2, "step_start", { mode: "answer" })];
  value.event_cursor = 2;
  await page.route("**/api/runs/math-preview/snapshot", route => route.fulfill({ json: value }));
  await page.route("**/api/runs/math-preview/events?*", route => route.continue({ url: `http://127.0.0.1:${address.port}/events` }));
  try {
    await page.goto("/?run=math-preview");
    await expect.poll(() => !!stream).toBe(true);
    const chat = page.locator("section").filter({ has: page.getByRole("heading", { name: "追问", exact: true }) }).last();
    const partial = String.raw`逐步推导：\[y=\frac{x+1}{2}`;
    const emit = (item: RunEvent) => stream!.write(`id: ${item.id}\nevent: ${item.type}\ndata: ${JSON.stringify(item)}\n\n`);
    emit(event(3, "assistant_text", { delta: partial }));
    await expect(chat.getByText("正在回答…", { exact: true })).toBeVisible();
    await expect(chat.locator(".katex")).toHaveCount(0);
    await expect(chat).toContainText("逐步推导");
    emit(event(4, "assistant_text", { delta: String.raw`\] 公式完成。` }));
    await expect(chat.locator(".katex")).toHaveCount(1);
    await expect(chat.locator(".katex-error")).toHaveCount(0);
    value.turns = [{ role: "assistant", text: partial + String.raw`\] 公式完成。`, status: "ok" }];
    value.active = false;
    value.event_cursor = 5;
    value.events = [];
    emit(event(5, "run_end", { status: "ok" }));
    stream!.write("event: stream_idle\ndata: {}\n\n");
    await expect(chat.getByText("正在回答…", { exact: true })).toHaveCount(0);
    await expect(chat.locator(".katex")).toHaveCount(1);
    await page.reload();
    await expect(chat.locator(".katex")).toHaveCount(1);
  } finally {
    stream?.end();
    server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
});
