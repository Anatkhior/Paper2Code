import { expect, test } from "@playwright/test";
import { resolve } from "node:path";

/** 使用本地假端点，绝不读取用户保存的 provider 或调用真实模型。先启动 dev_services。 */
test("原版 PDF 选择、页码与缩放，以及追问完整交付", async ({ page }, info) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.clear();
    localStorage.setItem("paperlens.provider.v1", JSON.stringify({
      protocol: "openai-compatible", base_url: "http://127.0.0.1:8123/v1",
      api_key: "mock-key", model: "chat-finish-intro", remember_key: true,
    }));
    localStorage.setItem("paperlens.timelineOpen", "0");
  });
  await page.goto("/");
  await expect(page).toHaveTitle(/Paper2code/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Paper2code");
  await expect(page.getByLabel("解析语言", { exact: true })).toHaveValue("zh-CN");
  await page.getByLabel("解析语言", { exact: true }).selectOption("en");
  await page.getByLabel("论文 PDF", { exact: true }).setInputFiles(resolve("../backend/tests/fixtures/synthetic_paper.pdf"));
  const created = page.waitForResponse((res) => res.url().endsWith("/api/runs") && res.request().method() === "POST");
  const recon = page.waitForRequest((req) => req.url().endsWith("/recon") && req.method() === "POST");
  await page.getByRole("button", { name: "上传并开始侦察", exact: true }).click();
  const createdResponse = await created;
  expect((await recon).postDataJSON().output_language).toBe("en");
  const { run_id: runId } = await createdResponse.json();
  const run = await page.request.get(`http://127.0.0.1:8000/api/runs/${runId}`);
  expect((await run.json()).meta.output_language).toBe("en");
  await expect(page.getByRole("heading", { name: /核心创新点/ })).toContainText("3 条");
  // 自动引用高亮和页面图像必须共享坐标面，提示文案不能改变它的高度。
  const highlight = page.locator(".pdf-highlight").first();
  await expect(highlight).toBeVisible();
  const highlightGeometry = await highlight.evaluate((element) => {
    const surface = element.parentElement!.getBoundingClientRect();
    const canvas = element.parentElement!.querySelector("canvas")!.getBoundingClientRect();
    return { dx: surface.x - canvas.x, dy: surface.y - canvas.y, dh: surface.height - canvas.height };
  });
  expect(Math.abs(highlightGeometry.dx)).toBeLessThan(1);
  expect(Math.abs(highlightGeometry.dy)).toBeLessThan(1);
  expect(Math.abs(highlightGeometry.dh)).toBeLessThan(1);
  await page.getByRole("button", { name: "浏览论文 / 划选目标 ↗" }).click();
  const layer = page.locator(".pdf-page .textLayer");
  await expect(layer.locator("span").first()).toBeVisible();
  await expect(page.locator(".pdf-page canvas")).toBeVisible();
  await expect(page.getByRole("button", { name: "PDF 原版", exact: true })).toHaveAttribute("aria-pressed", "true");

  // 真正拖动鼠标跨一行；官方文字层应与画布处于同一坐标面。
  const span = layer.locator("span").filter({ hasText: /[A-Za-z]{5}/ }).first();
  await span.scrollIntoViewIfNeeded();
  const box = (await span.boundingBox())!;
  await page.mouse.move(box.x + 1, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width - 1, box.y + box.height / 2, { steps: 12 });
  await page.mouse.up();
  const selected = await page.evaluate(() => window.getSelection()?.toString().trim());
  expect(selected!.length).toBeGreaterThanOrEqual(8);
  const added = page.waitForResponse((res) => res.url().endsWith("/plan/items") && res.request().method() === "POST");
  await page.getByRole("button", { name: "以此为目标定位代码", exact: true }).click();
  const addedResponse = await added;
  expect(addedResponse.status()).toBe(200);
  expect(addedResponse.request().postDataJSON()).toMatchObject({ page: 1, quote: selected });
  await page.getByRole("button", { name: "知道了", exact: true }).click();

  await page.getByRole("button", { name: "放大原文（两倍）" }).click();
  await expect(layer.locator("span").first()).toBeVisible();
  const geometry = await layer.evaluate((element) => {
    const text = element.getBoundingClientRect();
    const canvas = element.parentElement!.querySelector("canvas")!.getBoundingClientRect();
    return { dx: text.left - canvas.left, dy: text.top - canvas.top, dw: text.width - canvas.width };
  });
  expect(Math.abs(geometry.dx)).toBeLessThan(1);
  expect(Math.abs(geometry.dy)).toBeLessThan(1);
  expect(Math.abs(geometry.dw)).toBeLessThan(2);
  await page.getByRole("button", { name: "原文适合宽度" }).click();
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.locator(".pdf-page")).toHaveAttribute("data-page", "2");
  await expect(page.getByRole("button", { name: "以此为目标定位代码", exact: true })).toHaveCount(0);
  await expect(layer.locator("span").first()).toBeVisible();
  if (process.env.PAPER2CODE_CAPTURE_SCREENSHOTS) await page.locator("#paper-pane").screenshot({ path: info.outputPath("pdf-desktop.png") });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(layer.locator("span").first()).toBeVisible();
  if (process.env.PAPER2CODE_CAPTURE_SCREENSHOTS) await page.locator("#paper-pane").screenshot({ path: info.outputPath("pdf-mobile.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  await page.getByLabel("解析语言", { exact: true }).selectOption("zh-CN");
  await page.getByLabel("追问内容").fill("帮初学者详细理解这篇论文");
  const chatRequest = page.waitForRequest((req) => req.url().endsWith("/chat") && req.method() === "POST");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  expect((await chatRequest).postDataJSON().output_language).toBe("zh-CN");
  await page.getByLabel("追问内容").fill("下一条问题（只验证可以继续输入，不发送）");
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled({ timeout: 60_000 });
  const transcript = await page.request.get(`http://127.0.0.1:8000/api/runs/${runId}/chat`);
  const turns = (await transcript.json()).turns;
  expect(turns).toHaveLength(2);
  expect(turns[1].status).toBe("ok");
  expect(turns[1].partial).toBe(false);
  expect(turns[1].text).not.toBe("下面给初学者一份完整导读。");
  const chatPanel = page.locator("section").filter({ has: page.getByRole("heading", { name: "追问", exact: true }) }).last();
  await expect(chatPanel.getByText("下面给初学者一份完整导读。", { exact: true })).toHaveCount(0);
  await expect(chatPanel).toContainText(turns[1].text.replace(/\*\*/g, "").slice(0, 15));
  const lastParagraph = turns[1].text.split("\n\n").at(-1).replace(/\*\*/g, "");
  await expect(chatPanel.getByText(lastParagraph, { exact: true })).toHaveCount(1);
  expect(errors).toEqual([]);
});

test("解析语言独立保存，刷新后保留且窄屏可用", async ({ page }, info) => {
  await page.goto("/");
  const language = page.getByLabel("解析语言", { exact: true });
  await expect(language).toHaveValue("zh-CN");
  await language.selectOption("en");
  await page.reload();
  await expect(language).toHaveValue("en");
  if (process.env.PAPER2CODE_CAPTURE_SCREENSHOTS) await page.screenshot({ path: info.outputPath("language-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(language).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  if (process.env.PAPER2CODE_CAPTURE_SCREENSHOTS) await page.screenshot({ path: info.outputPath("language-mobile.png") });
});

test("PDF 加载失败时，高亮仍跟随回退图像而非提示文字", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("paper2code.provider.v1", JSON.stringify({
      protocol: "openai-compatible", base_url: "http://127.0.0.1:8123/v1",
      api_key: "mock-key", model: "mock-model", remember_key: true,
    }));
  });
  await page.route("**/api/runs/*/pdf", (route) => route.abort());
  await page.goto("/");
  await page.getByLabel("论文 PDF", { exact: true }).setInputFiles(resolve("../backend/tests/fixtures/synthetic_paper.pdf"));
  await page.getByRole("button", { name: "上传并开始侦察", exact: true }).click();
  await expect(page.getByRole("heading", { name: /核心创新点/ })).toContainText("3 条");
  await expect(page.locator(".pdf-page-notice")).toContainText("文字层加载失败");
  await expect(page.locator(".pdf-highlight").first()).toBeVisible();
  for (const zoom of [1, 2]) {
    if (zoom === 2) await page.getByRole("button", { name: "放大原文（两倍）" }).click();
    const dimensions = await page.locator(".pdf-highlight").first().evaluate((element) => {
      const image = element.parentElement!.querySelector("img")!.getBoundingClientRect();
      const surface = element.parentElement!.getBoundingClientRect();
      return { dx: surface.x - image.x, dy: surface.y - image.y, dw: surface.width - image.width, dh: surface.height - image.height };
    });
    for (const difference of Object.values(dimensions)) expect(Math.abs(difference)).toBeLessThan(1);
  }
});
