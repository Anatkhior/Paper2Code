import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const api = "http://127.0.0.1:8000";
const provider = { protocol: "openai-compatible", base_url: "http://127.0.0.1:8123/v1", api_key: "mock-key", model: "mock-model" };

test("历史无密钥回看、换模型续问、刷新和列表回读", async ({ page, request }, info) => {
  const pdf = await readFile(resolve("../backend/tests/fixtures/synthetic_paper.pdf"));
  const created = await request.post(`${api}/api/runs`, { multipart: {
    file: { name: "history-browser-test.pdf", mimeType: "application/pdf", buffer: pdf }, provider: JSON.stringify(provider),
  } });
  expect(created.ok()).toBeTruthy();
  const { run_id: id } = await created.json();
  await request.post(`${api}/api/runs/${id}/recon`, { data: { provider } });
  await expect.poll(async () => (await (await request.get(`${api}/api/runs/${id}/snapshot`)).json()).history.analysis_status).toBe("recon_complete");
  await request.post(`${api}/api/runs/${id}/locate`, { data: { provider, repo_url: resolve("../backend/tests/fixtures/sample_repo") } });
  await expect.poll(async () => (await (await request.get(`${api}/api/runs/${id}/snapshot`)).json()).history.analysis_status, { timeout: 60000 }).toBe("complete");
  const original = await (await request.get(`${api}/api/runs/${id}/snapshot`)).json();
  const posts: string[] = [];
  page.on("request", req => { if (req.method() === "POST") posts.push(req.url()); });
  await page.goto(`/?run=${id}`);
  await expect(page.getByRole("heading", { name: /核心创新点/ })).toContainText("3 条");
  await expect(page.getByLabel("历史解读信息")).toContainText("原模型：mock-model");
  await expect(page.getByRole("button", { name: "改名", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /开始定位选中的/ })).toHaveCount(0);
  await expect(page.locator(".pdf-highlight").first()).toBeVisible();
  expect(posts).toEqual([]);
  await page.getByRole("button", { name: /后续追问的模型/ }).click();
  await expect(page.getByLabel(/api_key（只放/)).toHaveValue("");
  await page.getByLabel(/base_url/).fill(provider.base_url);
  await page.getByLabel("模型名", { exact: true }).fill("chat-finish-intro");
  await page.getByLabel(/api_key（只放/).fill("mock-key");
  await page.getByLabel("追问内容").fill("根据已有创新点继续解释");
  const sent = page.waitForRequest(req => req.url().endsWith("/chat") && req.method() === "POST");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await sent).postDataJSON().provider.model).toBe("chat-finish-intro");
  await expect.poll(async () => (await (await request.get(`${api}/api/runs/${id}/snapshot`)).json()).turns.length).toBe(2);
  await expect(page.getByText(/chat-finish-intro · 127.0.0.1/)).toBeVisible();
  const after = await (await request.get(`${api}/api/runs/${id}/snapshot`)).json();
  expect(after.plan).toEqual(original.plan);
  expect(after.artifact).toEqual(original.artifact);
  expect(posts.filter(url => url.endsWith("/chat"))).toHaveLength(1);
  const count = posts.length;
  await page.reload();
  await expect(page.getByText(/chat-finish-intro · 127.0.0.1/)).toBeVisible();
  expect(posts).toHaveLength(count);
  await page.getByRole("link", { name: "历史记录", exact: true }).click();
  await page.getByRole("searchbox").fill("history-browser-test.pdf");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(page.getByRole("link", { name: original.history.title }).first()).toBeVisible();
  if (process.env.PAPER2CODE_CAPTURE_SCREENSHOTS) await page.screenshot({ path: info.outputPath("history-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  if (process.env.PAPER2CODE_CAPTURE_SCREENSHOTS) await page.screenshot({ path: info.outputPath("history-mobile.png"), fullPage: true });
});

test("修改模型撤销旧自检；切换供应商清空旧密钥", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("paper2code.provider.v1", JSON.stringify({
    protocol: "openai-compatible", base_url: "http://127.0.0.1:8123/v1", api_key: "mock-key", model: "mock-model", remember_key: true,
  })));
  await page.goto("/");
  await page.getByRole("button", { name: /运行自检/ }).click();
  await expect(page.locator(".smoke-compact")).toBeVisible();
  await page.getByLabel("模型名", { exact: true }).fill("another-model");
  await expect(page.locator(".smoke-compact")).toHaveCount(0);
  await expect(page.getByLabel(/api_key（只放/)).toHaveValue("mock-key");
  await page.getByRole("button", { name: "DeepSeek", exact: true }).click();
  await expect(page.getByLabel(/api_key（只放/)).toHaveValue("");
  await page.getByRole("button", { name: "存到本机浏览器", exact: true }).click();
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("paper2code.provider.v1")!).api_key)).toBe("");
});

test("离开上传页面后，迟到响应不改历史页地址或发起侦察", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("paper2code.provider.v1", JSON.stringify({
    protocol: "openai-compatible", api_key: "mock-key", model: "mock-model", remember_key: true,
  })));
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/runs", async route => {
    if (route.request().method() !== "POST") return route.continue();
    await gate;
    await route.fulfill({ json: { run_id: "late-upload", paper: { title_guess: "Late", page_count: 1 } } });
  });
  const mutations: string[] = [];
  page.on("request", req => { if (req.method() === "POST") mutations.push(req.url()); });
  await page.goto("/");
  await page.getByLabel("论文 PDF", { exact: true }).setInputFiles(resolve("../backend/tests/fixtures/synthetic_paper.pdf"));
  const uploading = page.waitForRequest(req => req.url().endsWith("/api/runs") && req.method() === "POST");
  await page.getByRole("button", { name: "上传并开始侦察", exact: true }).click();
  await uploading;
  await page.getByRole("link", { name: "历史记录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "历史解读", exact: true })).toBeVisible();
  release();
  await page.waitForTimeout(300);
  await expect(page).toHaveURL(/\/history$/);
  expect(mutations.filter(url => url.endsWith("/recon"))).toEqual([]);
});
