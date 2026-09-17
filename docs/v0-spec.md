# PaperLens v0 规格

> 本文档是 grilling 收敛后的结果，不是愿望清单。v0 只有一个目标：
> **证明"论文创新点 → 可核验代码引用"这条链路成立，并且能用数字衡量它有多准。**
>
> 凡是不能服务于这个目标的东西，一律推迟或砍掉（见 §13）。

---

## 0. 事实核查（先纠正原方案里不成立的前提）

| 原方案的说法 | 核查结果 | 处理 |
|---|---|---|
| 后端 FastAPI + Python **3.11+** | ⚠️ **我一开始判断错了，原方案是对的。** 本机 Python 3.10.12。虽然 PyPI 元数据上 `litellm 1.100.1` 写着 `requires_python >=3.10`，但它实际 `from typing import NotRequired`（3.11+ 才有），在 3.10 上直接 `ImportError` | **必须 3.11+**。本机已用 `uv python install 3.12` 装好 3.12.14，venv 已重建在 `.venv` |
| Agent 框架用 **Pi** | `earendil-works/pi` 是 **TypeScript** monorepo，`engines: node >= 22.19`，root 版本 `0.0.3`（各包 `0.85.1`），今天仍在推 commit；README 明说**没有内置权限/沙箱系统** | **弃用 Pi**，改用 `litellm` 提供统一多 provider 层 |
| LLM 用 **openai SDK** | 与 BYOK（用户自填 base_url + key + 模型）需求冲突：openai SDK 只覆盖 OpenAI 协议形状 | 统一走 `litellm`，协议差异收敛在 `providers.py` 一处 |
| 实时通信 **SSE + WebSocket 双通道** | 追问是"客户端发一次 → 服务端流式回"，普通 POST + 流式响应即可 | **砍掉 WebSocket**，全部走 SSE |
| 前端 **Next.js 14** | npm 上 `next` 最新稳定版 **16.3.4**（`react 19.3.0`、`tailwindcss 4.3.3`、`typescript 7.0.2`） | 用 `create-next-app` 生成最新稳定版，不照抄"Next.js 14" |
| **KaTeX 渲染论文公式** | PDF 文本层提不出 LaTeX（上下标丢失、希腊字母错位、分式塌行） | v0 公式 = **模型重构的 LaTeX，必须标注"重构，非原文"**；证据用"页码 + 原文片段"，不用解析公式 |
| **GitPython** | 只能负责 clone，不能负责安全 | 保留，但必须按 §10 硬化；仓库访问再包一层 `RepoSource` 接口 |

**其他核查**：本机 `node v22.23` ✓、`npm 10.9.8` ✓、`pnpm 12.3.4` ✓、**没有 docker**、没有 `uv`、没有安装 `pi`。
没有 docker 意味着"跑在容器里"这条隔离路线 v0 不可用 → 安全只能靠**进程内的硬化参数 + 不执行仓库代码**来保证（§10）。

---

## 1. 已定的决策

| # | 决策 | 理由 | 代价（已知并接受） |
|---|---|---|---|
| 1 | 前端 Next.js（最新稳定版）+ TS + Tailwind + shadcn/ui | 交互是主战场 | — |
| 2 | 后端 Python FastAPI + **LiteLLM** | 用户最熟 Python；LiteLLM 一行搞定 BYOK 多 provider | 前后端两种语言，流式要跨一次 HTTP 边界 |
| 3 | 传输：**单一 SSE 通道** | 少一套协议、少一类 bug | 放弃"双向实时"能力（v0 不需要） |
| 4 | 仓库：浅克隆到磁盘，但封装成 `RepoSource` 接口 | 磁盘上有目录才能 grep，探索质量最高 | 要处理不可信仓库 + 磁盘清理 |
| 5 | 产物：**结构化 mapping schema + 证据锚点** | 前端能渲染、能跳转、能机械核验 | 需要设计 schema 和校验器 |
| 6 | 公式：模型重构 LaTeX + 标注"重构" | v0 最省事且不撒谎 | 不能声称是论文原文 |
| 7 | 部署：**本地单用户**（别人 clone 下来自己跑） | 无密钥托管义务、无限流、无队列 | 作品集需要录屏 + 可跑命令来证明 |
| 8 | 评估：**5-10 对 gold set + 量化指标** | 没有数字就无法迭代 prompt | 前期多花 1-2 天做标注 |
| 9 | 交互：**两阶段**——Agent 先列创新点清单 → 用户勾选 → 再跑定位 | 用户能决定"我要看懂哪一块"，比系统自己猜更像工具而不是演示 | 多一次交互、多一个 UI 状态、多一套接口 |
| 10 | 仓库**不做官方性判断**，只锚定 commit 并在 UI 显示来源 | 判断"是不是论文作者写的"会猜错，而猜错比不猜更伤信任 | 用户需要自己看清仓库来源 |

---

## 2. v0 范围

**做**
- 单篇论文（PDF 上传）+ 单个公开仓库（URL），分**两阶段**完成（见下）
- 单 Agent + 一组原子工具（§5）
- `record_finding` 工具做**增量结构化输出**
- 落盘 `analysis.json`（§6）+ `events.jsonl`（§9）
- 一个 Next 页面：左侧行动时间线，右侧双栏对照（创新点 ↔ 代码），引用可点击跳到文件行
- 确定性校验 `verify.py`（§7）+ 覆盖率/预算护栏（§8）
- `eval/run_eval.py` + **2-3 对** gold set 起步（§11）

### 两阶段交互流程

```
阶段 A：recon（侦察）
  上传 PDF ──► Agent 只读论文，产出 3-5 条"待定位创新点"
              每条含：名称 / 一句话解释 / 论文证据(页码+原文) / 建议的搜索关键词
              ──► UI 列出清单，用户勾选（可全选），也可以自己补一条
              ▼
阶段 B：locate（定位）
  对每条被勾选的创新点，Agent 进仓库探索，产出 code_evidence
              ──► record_finding 增量推送 ──► verify.py 机械核验 ──► 双栏对照
```

**为什么要拆**：阶段 A 的上下文只有论文（便宜、几秒到几十秒）；阶段 B 只带"这一条创新点 + 仓库工具"（上下文干净）。
两段接口分开，用户还能在中间插手——这正是"让用户看懂他想懂的那一块"的产品价值。
阶段 B 在 v1 里会变成"每条创新点一个并行子任务"，接口不用改。

| 接口 | 作用 |
|---|---|
| `POST /api/runs` | multipart 上传 PDF + provider 配置，返回 `run_id` 与论文元信息 |
| `POST /api/runs/{id}/recon` | 启动阶段 A（立即返回），结束时发 `plan_ready` 事件 |
| `POST /api/runs/{id}/locate` | 阶段 B（M2），请求体带 `selected_ids` |
| `GET/POST/PATCH/DELETE /api/runs/{id}/plan/items…` | 阶段 C（v1-②）：用户自己指定/编辑定位目标 |
| `POST /api/runs/{id}/chat` | 阶段 D（v1-③）：追问一句（Agent 自己决定要不要查代码/论文） |
| `GET /api/runs/{id}/chat` | 取回对话记录（刷新页面恢复） |
| `GET /api/runs/{id}/events` | 所有阶段的统一事件流（SSE） |
| `GET /api/runs/{id}/file?path=&start=&end=` | 读某个 commit 上的一段代码（M3 点开引用用） |
| `GET /api/runs/{id}/paper/page/{n}` | 读论文某一页原文（M3 点开论文证据用） |
| `GET /api/runs/{id}/pdf` | 原样返回上传的 PDF（交给浏览器自带阅读器，真实排版） |

> 实现上的一个偏离：原计划让 `recon` 这个 POST 直接以 SSE 响应流式返回，
> 但浏览器的 `EventSource` **只能发 GET**。所以改成"POST 启动 + 统一的 GET 事件流"，
> 好处是断线重连、刷新回放、跨阶段续传全都免费拿到。

**不做（推迟）**
- 多 Agent 并行定位（v1）、追问对话（v1）、页面图像/视觉（v1）
- KaTeX 正式渲染（v0 只在代码块里显示模型重构的 LaTeX 文本）
- 多用户 / 鉴权 / 队列 / 计费 UI / 历史记录列表
- 扫描版 PDF OCR、CUDA/C++ 论文、私有仓库、多仓库、多论文对比

---

## 3. 目录结构

```
Paper2code/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI: /api/analyze, /api/runs/{id}/events, /api/runs/{id}
│   │   ├── config.py            # env: provider 描述符、预算上限、prompt_version
│   │   ├── providers.py         # BYOK：描述符 → litellm 调用；启动自检 smoke test
│   │   ├── budget.py            # 工具调用数 / input token / 时长 / 花费 护栏
│   │   ├── events.py            # 内存事件总线 → SSE 序列化 + events.jsonl 落盘
│   │   ├── store.py             # .data/<run_id>/{paper.pdf,repo/,analysis.json,events.jsonl}
│   │   ├── verify.py            # 确定性重放校验（§7）
│   │   ├── paper.py             # PDF 读取层：分页抽取 + 搜索 + 引文核验（不含任何"理解"）
│   │   ├── prompts.py           # 系统提示（含不可信内容声明）
│   │   ├── agent/
│   │   │   ├── loop.py          # 工具循环 + 流式 + 预算中断（目标 ~200 行）
│   │   │   ├── prompts.py       # （已上移到 app/prompts.py）
│   │   │   └── tools/
│   │   │       ├── base.py      # Tool / ToolContext / 截断 / safe_path 白名单
│   │   │       ├── builtin.py   # M0 的 ping / finish
│   │   │       ├── paper_tools.py  # list_pages / get_page_text / search_paper / read_paper_all
│   │   │       ├── repo_tools.py   # （M2）repo_tree / search_code / read_file
│   │   │       └── findings.py  # record_plan（阶段 A）/ record_finding（阶段 B）
│   │   └── repo_source.py       # RepoSource 接口（本地克隆实现；将来可换 GitHub API）
│   ├── eval/
│   │   ├── goldset.yaml         # 论文 + repo + commit + 人工标注
│   │   └── run_eval.py
│   ├── tests/
│   │   ├── paper_fixture.py     # 合成论文 PDF（英文，避开 PDF 内置字体的中文问题）
│   │   └── fixtures/
│   └── scripts/
│       ├── harness.py           # 验收脚本公用件（起服务 / 读 SSE / 断言计数）
│       ├── m0_check.py          # M0 验收（25 项）
│       └── m1_check.py          # M1 验收（66 项）
├── frontend/                    # Next.js
└── docs/
```

---

## 4. BYOK：provider 抽象与启动自检

**描述符**（前端表单 → 后端，一个对象搞定所有 provider）：

```python
class ProviderConfig(BaseModel):
    protocol: Literal["openai-compatible", "anthropic"]
    base_url: str | None      # 兼容端点必填；官方端点可留空
    api_key: str              # 只存在内存/请求里，不落库、不进日志
    model: str                # 例如 "deepseek-chat" / "gpt-4o" / "claude-sonnet-4-6"
```

**调用收敛在这一处**（`providers.py`）：

```python
resp = litellm.completion(
    model=resolve_litellm_model(cfg),   # 例："openai/deepseek-chat" / "anthropic/claude-..."
    api_base=cfg.base_url,
    api_key=cfg.api_key,
    messages=messages,
    tools=tool_schemas,
    stream=True,
    extra_headers={"User-Agent": settings.http_user_agent},   # 默认 "PaperLens/0.1"，可配
)
```

**为什么 User-Agent 要可配**（2026-09-13 实测）：个别套 Cloudflare 的第三方网关按 UA 把
openai-python SDK 形状的请求当 bot 直接 403（同 key 同请求体：SDK UA → 403 HTML，
自定义 UA / 浏览器 UA → 正常 JSON），请求根本到不了模型。所以 LLM 请求与端点探测
统一带 `PAPERLENS_HTTP_USER_AGENT`（默认 `PaperLens/0.1`，实测可通过；官方端点对 UA 无感），
自检的 HTML 错误页诊断里会提示这个开关。

**启动自检（必须做，否则整套架构会静默失效）**：分析开始前先跑一个两轮工具调用冒烟测试——
一轮要求模型调用一个无副作用工具（如 `ping(page=1)`），二轮要求它基于工具结果回答。
判定与失败信息：

| 失败现象 | 结论 | 给用户的提示 |
|---|---|---|
| 完全不返回 `tool_calls` | 端点不支持工具调用 | "该模型不支持 function calling，本项目无法工作" |
| 参数不是合法 JSON / 缺必填字段 | 端点 schema 支持差 | "该模型的工具参数不可靠，建议更换模型" |
| 流式响应缺 delta / 无 `finish_reason` | 流式兼容性差 | 提示可以试 `stream=False` 降级 |
| 通过 | 记录能力位（并行工具调用？视觉？） | 写进 `run.meta.capabilities` |

**版本策略**：`uv lock` 锁死全部依赖（当前 `litellm 1.100.1`）。LiteLLM 迭代很快，升级必须单独一次提交 + 跑完 gold set 再合。

---

## 5. 工具清单（v0）

设计铁律：**每个工具都必须能在返回超限时截断，并且明确标注 `[已截断]`**；`read_file` 必须支持行区间；搜索类工具**只返回"文件:行 + 命中行"**，绝不返回整个文件；所有路径参数经过白名单校验（必须落在本次 run 的 `paper/` 或 `repo/` 之下，拒绝 `..`）。

| 工具 | 参数 | 返回 | 预算与截断 |
|---|---|---|---|
| `list_pages` | — | 每页：字符数 + 首行（帮 Agent 判断结构，不做分节） | 小 |
| `get_page_text` | `page: int` | 该页纯文本 | 单页上限 ~4000 token |
| `search_paper` | `query: str` | 命中页 + 命中处上下文（±N 字符） | 最多 10 处命中 |
| `read_paper_all` | — | 全文文本（v0 允许！见下） | 上限 ~40k token，超限报错 |
| `repo_tree` | `path="", depth=2` | 目录树（文件名 + 大小 + 行数估算） | 最多 400 条 |
| `search_code` | `pattern, glob="**/*.py", max_hits=40` | `文件:行: 命中行` | 最多 40 条命中 |
| `read_file` | `path, start_line, end_line` | 指定行区间内容（带行号） | 单次上限 ~3000 token |
| `read_symbols` | `path` | 该文件的函数/类签名骨架（仅签名 + 行号） | 需 tree-sitter；**若时间紧可后置** |
| `record_finding` | 见 §6 | 校验后的确认回执 | 结构性工具，无 token 成本 |
| `finish` | `coverage_note` | 结束本次分析 | 终止循环 |

**关于"要不要逐页读论文"——一个反直觉但重要的结论**：
一篇 20 页论文全文约 2 万 token，很便宜。而**每次工具调用都要重发全部历史**，所以"翻 20 次页"的输入 token 总量近似二次增长，比一次性读完贵得多。
因此 v0 明确允许 `read_paper_all`：**把探索预算留给仓库，那才是真正贵的地方**（几十万到几百万 token）。
"Agent 自主决定读什么"的价值在代码侧，不在论文侧。

---

## 6. Artifact schema（v0 的核心交付物）

```json
{
  "schema_version": "0.1",
  "run": {
    "run_id": "…", "created_at": "…",
    "paper": { "title": "…", "source": "user_upload", "pdf_sha256": "…", "pages": 14 },
    "repo": { "url": "…", "commit_sha": "…", "default_branch": "main",
              "files_total": 312 },
    # 注意：不做 is_official 判断（§1 决策 10）。UI 只显示"引用基于此仓库此 commit"。
    "provider": { "protocol": "openai-compatible", "model": "deepseek-chat" },
    "prompt_version": "v0.1",
    "budget_used": { "tool_calls": 37, "input_tokens": 812345, "seconds": 214, "usd": 0.0 }
  },
  "innovations": [
    {
      "id": "inn-1",
      "name": "低秩重参数化（LoRA）",
      "one_liner": "把权重更新拆成两个小矩阵的乘积，只训练小矩阵。",
      "difficulty": "beginner",
      "paper_evidence": [
        { "page": 3, "quote": "…原文片段（截断到 300 字）…", "kind": "text" },
        { "page": 3, "kind": "equation", "latex": "h = W_0x + \\frac{\\alpha}{r}BAx",
          "latex_is_reconstruction": true }
      ],
      "code_evidence": [
        {
          "path": "src/models/lora.py",
          "symbol": "LoRALayer.forward",
          "line_start": 42, "line_end": 55,
          "snippet_sha256": "…",
          "commit_sha": "…",
          "why": "A/B 两个低秩矩阵的乘积在这里被加回主分支"
        }
      ],
      "explanation": {
        "intuition": "…",      "math": "…",
        "code_walkthrough": [ { "line_ref": "src/models/lora.py:42-55", "text": "…" } ],
        "read_next": ["…"]
      },
      "status": "matched",
      "confidence": 0.86,
      "confidence_reason": "论文公式与代码变量名、维度都能对上；但初始化方式论文未提",
      "verification": { "state": "pending", "checked_at": null, "failures": [] }
    }
  ],
  "not_found": [ { "name": "…", "reason": "论文提到的 X 在仓库中未找到实现", "searched": ["…"] } ],
  "coverage": { "pages_read": [1,2,3], "files_read": 23, "files_total": 312,
                "search_calls": 14, "stopped_reason": "agent_finished" }
}
```

**三条不变量（比 schema 本身更重要）**

1. 每条 `code_evidence` **必须**含 `commit_sha / path / line_start / line_end / snippet_sha256`，缺一不可——否则这条引用不合法。
2. `verification` 字段**只能由后端 `verify.py` 填写**，模型无权写入。防的是"模型自己宣布自己是对的"。
3. `status` 三态 `matched / partial / not_found`，**`not_found` 是合格输出而非失败**；`confidence` 必须附 `confidence_reason`。
4. **论文引文同样要被机械核验**（M1 已实现）：`paper_evidence` 的 `quote` 必须真的出现在它声称的那一页，
   由 `PaperDocument.quote_match()` 分级判定（忽略空白差异）：`full` = 逐字出现在那一页；
   `partial` = 只有开头 60 个（折叠空白后的）字符匹配——容忍模型轻微抄错，但 `evidence.quote_match = "partial"`
   必须如实标注，界面上显示「部分匹配」，**不能让"真开头 + 编造后半段"冒充逐字引用**；
   `none` = 找不到。`quote_found()` 是它的布尔包装（full/partial 都算"找到"）。`none` 时 `record_plan`
   会把错误**回给模型重做一次**；第二次仍不合格则接受提交但标记 `verified: false`（给它改正机会，但不许无限重试烧钱）。
5. **用户加的定位目标不会被后续侦察冲掉**：`record_plan` 提交时把 plan.json 里已有的
   `source: "user"` 条目并入新清单（2026-09-12 补：先划选、再侦察原先会静默清空用户目标）。

---

## 7. 确定性校验（`verify.py`）

对每条 `code_evidence` 依次检查，全部通过才 `state = "verified"`：

1. `commit_sha` 在仓库中存在（`git cat-file -e`）
2. `path` 在该 commit 下存在（`git cat-file -e <sha>:<path>`）
3. 行区间有效（`1 <= start <= end <= 文件行数`）；行号口径与 `read_file`（Agent 看到的）、
   `/file`（前端看到的）**三处统一**为 `repo_source.text_lines()`（按 `splitlines()` 数）——
   文件开头/结尾有连续空行时三者也必须一致，否则 Agent 照工具返回的行号提交的引用会被误判为非法
4. 取 `git show <sha>:<path>` 的 `[start, end]` 行，做**归一化**（去行尾空白、统一换行、去首尾空行）后计算 sha256，与 `snippet_sha256` 相等

任一条失败 → `state = "failed"` + `failures[]` 记录原因，前端把该引用标红为"未通过核验"。
指标：`citation_verifiable_rate = verified 引用数 / 全部引用数`。**这是本项目的头号指标。**

---

## 8. 运行治理（预算、取消、幂等）

| 项 | v0 默认值 | 超限行为 |
|---|---|---|
| 工具调用次数 | 40 | 停止探索 → 用已有 findings 出 artifact |
| input token 累计 | 1.5M | 同上 |
| 墙钟时间 | 10 分钟 | 同上 |
| 单次花费（可选） | 由 provider 报价估算，默认不限 | 同上 |
| 单文件读取 | 3000 token | 工具层截断并标注 |

- **预算必须对模型本人可见**（2026-09-15 补）：实测有一轮 23 轮 / **40 次工具调用全部花在探索上**、
  一条结论都没提交（核验 0/0）。"超限不是崩溃、而是用已确认的部分交付"这条原则，
  **前提是模型得知道快超限了**。所以：系统提示词把"边搜边交"写成硬性要求；循环在用到
  60% / 85% / 墙钟告急时**注入一条 user 角色的 `[预算提醒]`**（带用量与明确动作：立刻
  `record_finding` 提交已确认的、找不到的写 `not_found`，不要再开新方向），并同时作为
  `budget_warning` 事件显示在时间线上。用 user 角色而不是中途插 system，是为了兼容
  不接受对话中途 system 消息的网关。
- **降级不是崩溃**：任何超限都走"用已确认的部分生成 artifact + 写明 `stopped_reason` + coverage 声明"。
  **失败也同理**（2026-09-15 补）：run 中途因为端点问题抛错时，也要把已经记录下来的结论核验、
  落盘并发出 `verification_done`，`run_end` 标 `partial=true` —— 用户等了半天（可能还花了钱），
  不能因为第 20 轮撞上限流就把前 19 轮的成果全丢掉。
- **端点限额**（2026-09-15 补，两轮）：一次定位/侦察要几十次 LLM 调用（一次 turn 一次请求），
  而实测某中转站限制「1 分钟最多 10 次，包括失败次数」——20 轮只花 61.6s ≈ 19.5 次/分钟，
  必然撞限流。判据按「越靠前越权威」分层，目标是**让用户不需要知道自己的限额**：
  1. **响应头**（不需要用户知道任何东西）：成功响应读
     `x-ratelimit-limit-requests: 10, 10;w=60`（上限, 突发; 窗口秒）与 `x-ratelimit-limit-tokens`；
     失败响应读 `retry-after`。**注意取头的路径**：流式响应要从
     `response.completion_stream.response.headers` 取，异常要从 `exc.litellm_response_headers` 取
     —— `exc.response.headers` 是空的（实测，写错过一次）。
  2. **网关话术**：`1分钟内最多请求10次` / `200000 tokens per minute`（中英文、双向语序都认）。
  3. **配置**：`PAPERLENS_LLM_MAX_REQUESTS_PER_MINUTE=N`（兜底，不再是必须）。
  4. **兜底阶梯**：窗口感知（10s 起、翻倍到 90s），并按**总等待预算**（180s）而不是「重试 5 次」计数。
  - **TPM 单独处理**：token/分钟 的限制靠拉长请求间隔没用 → 按最近 60s 已发送 token 节流
    （`_await_llm_slot` 的 token 分支），并且只重试一次（反复空等没有意义）。
  - **失败分类**（决定要不要重试）：`rate_limit`（退避重试）/
    `token_limit`（按 token 节流 + 重试一次 + 建议减少上下文）/
    `quota_exhausted`（402 或 429+配额/余额：**不重试**，直接说「重试无用：去充值/换 key」）/
    `transient`（连接错误 / 断连 / 网关 5xx：**退避重试**，5s 起跳比限流更快——实测用户
    跑到第 10 轮时 `Connection error.` 让整轮作废，而这类错误重试往往就好了）。
    超时**刻意不算** transient：那是「端点太慢」，重试只会在同一时限上再等一遍，
    而 `per_turn_timeout_seconds` 是用户设的护栏（自检也靠它判定慢端点）。
  - **中途断线不静默成功**：流到一半连接断了，只有在**还没收到任何内容**时才整轮重试
    （已吐给前端的文字收不回来，重来会让时间线出现两份重复的中间过程）；
    已经收到内容的如实失败，靠「失败也交付」保住已确认的结论。
  - 诊断文案按类分开，且**分支顺序有意义**：`transient` 必须排在通用的
    `connection` 判断之前（实测踩过：`MidStreamFallbackError` 的消息里也带 "connection"，
    被通用分支抢走后，用户看到的解释与处置建议都是错的）。
    「连接被拒绝 / 域名解析失败」单独走「地址或端口不对」的建议。
  - 学到的节奏**按端点持久化**（`data/llm-pace.json`，只存节奏不存任何密钥），重启不用重学；
    `/api/health` 的 `llm_pacing` 与**自检结论**都会报出「当前节奏 + 来源（配置/响应头/话术/学到的）」。
  - 等待都会通过 `llm_retry` 事件显示（≥10s 才提示，避免刷屏）；`run_start` 的 limits 里也带当前节奏。
  - **预算可行性预警**：`节奏 × 剩余调用 > 剩余墙钟` 时提前 `budget_warning`——
    否则修好限流只是把用户推到「预算用尽」那堵墙上；预算本身不自动放宽（那是用户设的护栏），
    但停下来的原因会注明「其中 N 秒花在端点限额等待上」。
  - **关掉 SDK 自己的隐式重试**（`num_retries=0`）：实测 openai SDK 默认会静默重试 429，
    于是「撞了限流」在时间线上完全看不见，而且它不认识我们学到的节奏。
- **取消**：`POST /api/runs/{id}/cancel` → 中断 LLM 流 + 终止 git 子进程 + 标记 run 为 `cancelled`（已产出的 findings 仍然交付）。
- **幂等 / 缓存**：`run_key = sha256(pdf_sha256 + repo_url + commit_sha + prompt_version + model)`。命中则**回放 `events.jsonl`**，用户看到完整时间线且不花一分钱。
- **版本锚定**：artifact 里必须固化 `commit_sha`，并在 UI 上显示。否则三个月后你的解读全部腐烂。

---

## 9. 事件契约（SSE）

`GET /api/runs/{id}/events`（`sse-starlette`），每条事件带单调递增 `id`，支持 `Last-Event-ID` 断线重连；同时追加写入 `events.jsonl`，所以**刷新页面能完整回放**。

| 事件 | 载荷 | 前端用途 |
|---|---|---|
| `run_start` | paper/repo/provider 摘要、阶段（recon / locate）、预算上限 | 初始化页面 |
| `step_start` | 轮次 | 时间线分段 |
| `assistant_text` | `delta` 文本增量 | 时间线里的打字机输出 |
| `tool_call` | 工具名 + 参数摘要 | "正在搜索 `LoRA`…" |
| `tool_result` | 结果摘要 + 耗时 + 是否出错 | 展开可见详情 |
| `plan_ready` | 阶段 A 的产出：创新点清单 | **弹出勾选界面** |
| `repo_cloning` | 正在克隆的地址 | "正在克隆仓库…" |
| `clone_progress` | 克隆实时进度（git --progress 的 stderr 行，限频转发） |
| `repo_ready` | commit / 文件数 / 体积 | 显示锁定到哪个 commit |
| `finding` | 一条完整的 innovation 对象 | **右侧对照栏实时增量出现** |
| `verification_done` | 引用核验统计（核验率、失败清单、缺失的 id） | 顶部显示核验率 |
| `budget_warning` | 已用 / 上限 | 黄色提示条 |
| `error` | 类型 + 人类可读信息 | 红色提示 + 重试按钮 |
| `run_end` | run 状态 + 统计 + `stopped_reason` | 结束态 + 显示"复制 run_key" |

（`assistant_text` / `plan_ready` 是 M0 定稿时补上的；`repo_cloning` / `repo_ready` / `verification_done` 是 M2 补上的。）

**一条硬性约束**：`run_end` 必须是最后一条事件，前端看到它就收工关流。
所以任何"要给用户看的结果"都必须在它之前发出去 —— 核验统计走的是 `run_agent` 的 `finalize` 钩子
（这个坑是 M2 验收脚本抓到的：`verification_done` 一度发在 `run_end` 之后，浏览器永远看不到它）。

**前端只能渲染"可观测行动轨迹"**（工具名、参数、结果摘要、阶段结论）。OpenAI/Anthropic 的 API **不返回可展示的原始思维链**——需求文档里"实时展示 Agent 的思考过程"必须改写为"展示 Agent 的行动轨迹与阶段性发现"，否则验收时对不上。

---

## 9.5 前端版式与阅读器行为（2026-09-16 按用户反馈调整）

- **动作入口只有一个**：定位按钮只出现在「代码仓库」那一节（它需要仓库地址），
  创新点清单区不再有第二个同名按钮。
- **页面顺序**：… 5. 行动轨迹 → 6. 对照阅读器 → 7. 追问 → 8. 逐条结论。
  追问夹在阅读器与结论之间，便于"边看边问"；结论区引导文案指向"上方的对照阅读器"。
- **点结论里的引用**：页面滚到对照阅读器，同时
  - 左栏（论文）：按页码取该页原文，并把引文高亮出来；**高亮会自动滚到可视区中间**，
    用户不需要自己翻（`<mark>` + `scrollIntoView({block:"center"})`，在引文/页/视图变化时触发）；
  - 左栏切到「PDF 原版」时：显示**服务端渲染的该页**（PyMuPDF → PNG，带磁盘缓存），
    并在上面叠**引文高亮框**——矩形由后端 `page.search_for()` 定位（与引文核验同一个库、
    同一套文本口径），前端按页面尺寸换算成百分比，缩放/换屏都不会错位。
    为什么不用内嵌浏览器 PDF 阅读器：它不允许外部脚本操作内部 DOM，`#search=` 在 Chrome 上
    不生效（2026-09-16 用户实测：PDF 那边完全没高亮）→ 改成「渲染图 + 自己画框」，
    任何浏览器表现一致，公式与图仍是原样渲染。定位不到就**不画框**并如实说明（不许放假框）；
    想看原生阅读器有「在新标签页打开原版 PDF」的入口。
  - 右栏（代码）：跳到引用行并高亮（原有行为）。
- **自动滚动只许动自己的容器，绝不许动窗口**：时间线与追问面板的「跟到最新」原来用
  `scrollIntoView({block:"end"})`，而它会一路滚所有可滚祖先（包括 window）——
  追问产生新事件时页面会被拽到时间线（页面上方），用户正在看的追问面板被顶走
  （2026-09-16 用户实测）。现在改成只设容器的 `scrollTop = scrollHeight`，
  并且**只有用户本来就在底部**时才跟随（否则会打断他往回翻看历史）。
- 断言分布：版式与顺序、`highlight_rects` / 渲染图端点在 `m3_check`（静态 HTML + 后端接口），
  阅读器与滚动的实现标记在 `m6_check`（打包产物层面，含「旧写法已消失」的反向断言）。

## 10. 安全清单（不可跳过）

**仓库克隆硬化**（`repo_source.py` 内集中实现）：

- **`git ls-remote` 预检**（2026-09-12 补）：克隆前先一次往返确认"仓库存在、HEAD 是哪个
  commit"，失败立刻给人话（不用等整个 clone 磁到超时）；拿到的 sha 同时作为缓存键
- **(URL, HEAD sha) 克隆缓存**（2026-09-12 补）：`data/repo-cache/<hash>/`，命中时走本地
  文件系统克隆（对象硬链接，秒级，不再走网络）；因为产物钉死 commit，同 (URL, sha) 的
  复用与重新克隆语义完全等价。缓存条目上限 32 个（按 mtime 淘汰）；`clean` 命令不碰它。
  依据（实测）：从浅克隆做本地克隆是允许的，产物同样是钉在同一 commit 的单提交仓库
- `git -c core.hooksPath=/dev/null clone --depth 1 --no-tags --single-branch <url>`
- **只取"源码视图"（2026-09-14 重做）**：`--filter=blob:none`（只要提交与目录树）+ `--sparse`
  （只检出源码类文件），排除规则 = `SKIP_DIRS` + `BINARY_SUFFIXES` + 仓库 `.gitattributes` 里
  声明为 `binary`/`filter=lfs` 的模式 + `PAPERLENS_REPO_SPARSE_EXCLUDE`。用
  `PAPERLENS_REPO_SPARSE=false` 退回全量克隆。
  起因：原来 `--depth 1` 全量检出 + "体积上限当代码规模上限"，实测 `zju3dv/INTACT-JEPA`
  为了读 121 个文本文件（1.8MB）下载了 **237MB**（工作区 140MB 里 6 个 `docs/assets/*.mp4`
  演示视频占 83MB，另有 `.git` 97MB 是同一份内容的第二份拷贝）→ 直接撞 200MB 上限被拒。
  改后同一仓库 **2.3MB / 10s**，源码一个不少、0 个视频。
  两个必须知道的 git 事实（都实测过）：`git ls-tree -l` 会为了拿文件大小**把 blob 全拉回来**
  （`.git` 284KB → 99MB，所以只读树一律 `ls-tree -r` 不带 `-l`）；**任何检出都会强制拉回所有
  blob**（`--filter=blob:limit=1m` + 检出 = 141MB，比全量还慢），所以只能靠稀疏规则在检出前过滤。
  服务器不支持 `--filter` 时 git 只警告不报错（`filtering not recognized by server, ignoring`），
  此时退化为"全量对象库 + 稀疏工作区"，如实按 disk 口径计量并提示。
- **体积口径拆开**：`files_total`/`mb` 是**工作区**（Agent 看得见的源码视图），
  `git_mb` 是对象库，`disk_mb = 两者之和`；`repo_max_mb` 管的是 `disk_mb`。
  被跳过的文件数经 `git ls-files -t`（`S` = skip-worktree）如实上报，并说明原因。
- **被拒的仓库不留痕**：体积检查在回填克隆缓存**之前**执行（原顺序会让被拒仓库照样占 238MB）
- `GIT_LFS_SKIP_SMUDGE=1`（不下 LFS）、**绝不 `--recurse-submodules`**
- `GIT_TERMINAL_PROMPT=0`、`GIT_ASKPASS=/bin/true`（避免卡在交互提示）
- **git 配置隔离跟部署模式走**（2026-09-14 修正）：`local`（默认）**继承**用户的
  `~/.gitconfig` 与系统配置——企业用户正是靠 `http.proxy` / `http.sslCAInfo`（MITM 代理的 CA）/
  `insteadOf`（内网镜像）才能克隆；隔离掉他们**永远跑不通且报错指不到真因**
  （实测：`GIT_CONFIG_GLOBAL=/dev/null` 下 `git config --global --get http.proxy` 读不到）。
  `hosted` 才设 `GIT_CONFIG_GLOBAL=/dev/null` + `GIT_CONFIG_NOSYSTEM=1`（防 `insteadOf`
  把白名单域名改指向别处）。用 `PAPERLENS_REPO_ISOLATE_GIT_CONFIG` 可显式覆盖。
- 超时（默认 300s——2026-09-13 从 60s 放宽：实测经本机代理克隆 GitHub，7MB 小仓库
  的下载也会超过 60s；可用 `PAPERLENS_REPO_CLONE_TIMEOUT_SECONDS` 调整）+ 克隆后 `du` 检查大小上限（默认 200MB）
- **克隆进度可视化**（2026-09-13 补）：`git clone --progress` 的 stderr 按行（兼容 `\r` 刷新）实时转发为 `clone_progress` 事件，慢下载不再是黑盒；克隆在线程池里跑，不再阻塞事件循环。
**定位可重入**（2026-09-12 补）：同一个 run 第二次点「开始定位」（换勾选、划选补目标、失败重试）
  时，`clone_repo(..., overwrite=True)` 先清掉旧的 `repo/` 再重新克隆——`repo/` 是派生数据，
  重新克隆没有信息损失；没有这条路径，第二次定位永远撞「目标目录已存在」
- **URL 白名单**：仅 `https://`；主机白名单默认 `github.com` / `gitlab.com`，条目可写
  `host:port`（自建 GitLab 常用 8443），用 `PAPERLENS_REPO_ALLOWED_HOSTS` 覆盖
  （接受逗号分隔或 JSON；报错文案会给出可直接复制的写法）
- **SSRF 守卫的判据（2026-09-14 重做）**：原来拿"本机 DNS 的答案"一票否决，但挂了 TUN +
  fake-ip 的代理（Clash/Mihomo/sing-box/Surge）或企业分流 DNS 时，**本机解析结果是代理
  占位地址而不是目的地**（实测：`github.com → 198.18.0.47`，而公网 DoH 说 `140.82.113.3`，
  `git ls-remote` 与真克隆都正常）。而且"猜网段"不泛化：198.18/15 被拦、28/8 侥幸放过、
  240/4 被拦、NAT64 的 `64:ff9b::/96` 因 `is_reserved` 也被拦——同类使用者的命运取决于
  代理厂商设了什么段。改成三层：
  1. **部署模式决定严格度**：`PAPERLENS_REPO_NETWORK_MODE=local`（默认，单机自用）只拒绝
     回环/链路本地/组播/未指定这类"绝不可能是代码托管站"的地址（云元数据
     `169.254.169.254` 就在链路本地段里），非公网解析结果只记提示、不阻断；
     `hosted`（公网多租户）把非公网解析结果当拒绝项。
  2. **DoH 交叉核验**（`PAPERLENS_REPO_DNS_CROSSCHECK=auto|on|off`，端点
     `PAPERLENS_REPO_DOH_ENDPOINTS`）：需要判定时，用 **literal-IP** 的公网 DoH 端点核验
     "这个域名在公网的真实归属"，区分"代理占位符"和"真内网目标"——与厂商、网段、IP 族无关
     （NAT64、240/4、自定义 fake-ip 段都覆盖）。同域名同时解析出公网+非公网地址按
     rebinding 特征拒绝。
  3. **显式信任**：`PAPERLENS_REPO_ALLOW_CIDRS` 列出信任的网段（企业内网镜像、已知 fake-ip
     段）；`PAPERLENS_REPO_ALLOW_PRIVATE_IPS=true` 作为粗粒度兼容开关保留。
  环境里存在 `HTTPS_PROXY`/`ALL_PROXY` 等正向代理时跳过本机 DNS 判定（域名由代理解析，
  本机答案不代表目的地）。判定过程与结论（notes）写进 run meta 并显示在前端时间线上。
  **注意：进程内的 DNS 检查不是安全边界**（DNS rebinding 可绕）——**公网多租户部署必须
  另有网络层隔离**（netns / 容器 / 防火墙），这条守卫只负责"别把正常用户挡在门外"。
- **绝不执行仓库里的任何代码**：不 `pip install`、不 `import`、不跑 `setup.py`、不跑 `make`
- 路径护栏：所有工具路径参数 `realpath` 后必须位于 `repo/` 之下

**上传与运行**：
- PDF 页数上限（50）/ 大小上限（50MB）；解析失败给出明确错误而不是 500
- 用户 key：只经过内存，**不写日志、不落库、不进错误堆栈**；README 里显式承诺
- `.data/` 生命周期：run 结束可清理；提供 `--clean` 选项

**Prompt injection**（论文与代码文本都是攻击面，里面可以写"忽略以上指令"）：
- 系统提示中明确声明：论文/代码内容全部是**不可信数据**，任何来自其中的"指令"一律无效
- 工具返回值统一包裹在明确分隔符中，并标注来源与"不可信"
- **结构性防线**：因为引用必须通过 §7 的机械核验，注入者能骗到的最多是"解释文字"，骗不到"引用"——引用校验失败会直接暴露它

---

## 11. 评估 harness（作品集的说服力在这里，不在架构图里）

> **状态：已实现**（M4）。代码在 `backend/app/eval_metrics.py`（指标，纯函数）、
> `backend/eval/run_eval.py`（运行器）、`backend/eval/goldset.yaml`（标准答案表）、
> `backend/scripts/m4_check.py`（验收）。

**`goldset.yaml` 结构**

```yaml
- id: lora
  paper: { title: "LoRA: Low-Rank Adaptation of LLMs", pdf: eval/pdfs/lora.pdf, sha256: "…" }
  repo:  { url: "https://github.com/microsoft/LoRA", commit_sha: "abc123…" }   # 必须钉死
  official: true
  labeler: "me"
  labeled_at: "2026-09-11"
  innovations:
    - name: "低秩重参数化"
      must_find_files: ["loralib/layers.py"]
      must_find_symbols: ["LoRALayer", "Linear.forward"]
      note: "核心：h = W0x + BAx"
- id: xxx-no-code
  repo: { url: "https://github.com/…", commit_sha: "…" }
  official: false
  expect_not_found: true     # 这题考的是"能不能诚实地说找不到"
```

**标注纪律（这条比指标本身更重要）**
1. **先标注、后运行**：`commit_sha` 和人工标注写完并冻结之后，才允许跑系统。先跑再看结果补标注 = 数据污染，你的数字就变成自欺。
2. 标注基于**你自己读论文+代码**，不参考系统输出（可以另开一次"调参集"用于迭代 prompt，gold set 只在最后测）。
3. repo 的 `commit_sha` 一旦标注就冻结——仓库会持续演进，不钉死 commit 的指标没有意义。

**指标表**

| 指标 | 定义 |
|---|---|
| `file_recall` / `file_precision` | 定位到的文件 vs `must_find_files` |
| `symbol_recall` | 定位到的函数/类 vs `must_find_symbols` |
| `citation_verifiable_rate` | §7 校验通过率（头号指标） |
| `honest_not_found` | 在"无实现"论文上是否正确输出 `not_found` |
| `p50 / p95 时长`、`平均工具调用数`、`平均 input token`、`单次成本` | 工程与成本 |

起步只要 **2-3 对**（1 对经典 PyTorch + 1 对你读过的 + 1 对无官方实现的）。指标写进 README 顶部。

---

## 12. 里程碑（按"能验证什么"排序，不按"写了多少代码"）

| | 内容 | 完成判据 | 状态 |
|---|---|---|---|
| M0 | BYOK + 启动自检 + SSE 事件契约 + `/api/analyze` 骨架（单 Agent 双工具） | 离线 mock 验收脚本全绿 | ✅ 完成（`m0_check` 25 项） |
| M1 | **阶段 A**：PDF 上传 → 论文工具 → 创新点清单 → `plan_ready` → 前端勾选 | 时间线能看到工具调用过程；清单可勾选 | ✅ 完成（`m1_check` 66 项） |
| M2 | **阶段 B**：克隆 → 仓库工具 → `record_finding` → `verify.py` | 校验率可计算；能输出 `not_found` | ✅ 完成（`m2_check` 76 项） |
| M3 | 双栏对照 UI + 引用点击跳文件行 + 覆盖率/预算展示 | 一条引用能点开看到代码原文 | ✅ 完成（`m3_check` 28 项） |
| M4 | gold set + `run_eval.py` + 数字进 README | 屏幕上有可复现的数字，且有标注纪律 | ✅ 完成（`m4_check` 48 项） |
| M5 (v1-①) | **双栏阅读器**（左栏 PDF 原版/原文文本可切、右代码，各自滚动、引用高亮）+ 解释全字段渲染 + 解释质量门槛 | 引用点开即在大窗里看到原文与代码 | ✅ 完成 |
| M6 (v1-②) | **在原文里划选 → 加为定位目标**（用户自定义创新点，可编辑后跑定位） | 用户能指定"我想看懂这一段" | ✅ 完成（`m6_check` 32 项） |
| M7 (v1-③) | **追问对话**（Agent 可重新查代码/论文；每条消息工具调用上限；前端显示它查了什么；对话落盘可回放） | 对一条引用能连续追问细节 | ✅ 完成（`m7_check` 42 项） |
| M8 (v1-④) | 每条创新点并行定位、页面图像（公式/架构图）、多 provider 兼容性矩阵 | — | 待做 |

**一键复跑全部验收**（脚本用 8231/8232，和手动调试的 8123/8000 互不干扰）：

```bash
cd backend && ./scripts/run_all_checks.sh
```

**M4 阶段实际落地的额外约束**：
- **指标必须能被证伪**：`m4_check` 拿手工构造的"答错产物"喂给指标函数，
  断言答错时分数会掉（指向错文件 → 召回 50%；文件对符号错 → 符号召回 0；
  本该报"没有实现"却编了一个 → 诚实性 0）。只会输出 100% 的指标等于没有指标。
- **编造的引用要计入准确率分母**：本该"没有实现"的条目上给出的代码引用属于无中生有，
  计入 `file_precision` 的分母（但永不算命中）—— 否则编造不会被惩罚。
- **离线数字旁边必须写明局限**：`mock` 那一行由脚本化"理想 Agent"跑出来，
  只证明管道与指标没写错，不代表真实模型能力；README 与 report.md 都强制标注这一点。

**PDF 原版 vs 原文文本（v1 修订）**：
- 抽出来的文本**不是原排版**：公式会塌、图会丢。所以左栏给了两种看法：
  **PDF 原版**（浏览器自带阅读器，滚动/缩放/翻页/搜索都在，公式和图都在）与
  **原文文本**（用来做引文高亮、以及划选加目标）。
- 为什么不让 PDF 阅读器兼任划选：PDF 阅读器在 iframe 里，里面的文字选中事件拿不到外层，
  所以"划选加目标"必须走文本视图。界面上直接写明了这一点，不让用户猜。

**阶段③（v1）实际落地的设计取舍**：
- **对话的每条消息有独立预算**（默认 8 次工具调用 / 40 万 token / 180 秒），
  再问一句也不会吃掉整个 run 的额度。预算耗尽时仍然给用户一句交代，而不是静默失败。
- **回答里的代码位置会被机械核对**：回答是自由文本，但里面每个 `文件:行号`
  都被抽出来拿去 `git show` 核对（文件在不在、行号超没超范围），核不过的在界面上标红。
  这是把"引用可核验"这条主线延伸到对话里——追问不能变成新的幻觉温床。
- **每轮都显示"它这次查了什么"**：用户可以据此判断这回答是查证过的还是凭印象说的。
- 对话落盘在 `chat.jsonl`：刷新页面能恢复，下一轮也能带上历史（用一个"能作证"的假端点行为验证过，
  不是靠读代码猜）。

**阶段②（v1）实际落地的设计取舍**：
- **清单是可变状态，就存在 `plan.json` 的同一个 plan 对象上**：阶段 B 读的就是它，
  所以用户加的目标天然会被定位，不需要另开通路。
- **没跑侦察也能加目标**：用户想"只看我指定的一段"时，不该被流程拦住。
- 从划选原文里自动挑搜索线索时**只保留形如 `lora_alpha` / `self.scaling` / `LoRALayer` 的词**；
  普通英文单词（write、training）当线索只会误导，挑不出来就留空、让 Agent 自己判断。
- 用户输入的引文**同样过核验**，但核验不通过**不拒绝**（他可能在手打描述），只标 `verified=false`。

**M3 阶段实际落地的两条设计约束**：
- 前端"点开看原文"读的代码，和 `verify.py` 核验的代码**必须是同一份**：
  两个端点都走 `RepoSource.content_at_commit()`（git 对象），不读工作区。
  否则会出现"你看到的"和"被核验的"不是同一个东西，核验就白做了。
- 覆盖率与预算卡片常驻：读过哪几页、仓库里读了几/共几个文件、工具调用与 token 用量、停止原因、引用核验率。
  用户要靠这些数字判断"这份解读可信到什么程度"——"读了 312 个文件中的 40 个"和"读了 2 个"是两回事。

**M2 阶段实际落地的额外护栏**：
- 仓库访问全部收在 `repo_source.py`：URL 白名单 + SSRF 内网拦截 + 浅克隆禁 hooks/submodule/LFS + 体积/超时上限 + **绝不执行仓库代码**
- `snippet_sha256` **由后端算**，模型无权提供；核验从 `git show <sha>:<path>` 读（不读工作区），所以"重放"是可信的
- 产物里锚定 commit，几个月后仓库变了也不影响已有解读
- 找不到实现是一等公民结果：`not_found` 必须附"为什么"和"搜过什么"

**M1 阶段实际落地的额外护栏**（原规格里没有、实现时补上的）：
- 论文引文的机械核验（`quote_found`，忽略空白差异）+ 「打回一次重做」的机制
- `read_paper_all` 的一次性读取策略（短论文一次读完比逐页翻更省，因为每次工具调用都要重发全部历史）
- 分页文本缓存按 **PDF 内容哈希**分命名空间（防止同一路径换文件后命中旧缓存，静默返回错的正文）
- 前端默认不持久化 api_key，要存必须显式勾选

---

## 13. 明确不做（v0 的护身符）

扫描版 PDF / OCR；CUDA / C++ / Rust 代码；私有仓库与鉴权；多仓库、多论文对比；引用图谱；自动生成可运行代码；多人协作与账号体系；公网部署与限流；WebSocket；Celery/Redis 队列；KaTeX 正式渲染；"分析历史"列表页。

**一句话判据**：任何一项，如果它不能让 `citation_verifiable_rate` 或 `file_recall` 变得更好看，它就不属于 v0。
