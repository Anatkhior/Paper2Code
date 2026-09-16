# PaperLens

上传一篇论文 PDF + 粘贴一个 GitHub 仓库链接，系统用 Agent 自主探索，定位论文核心创新点对应的代码实现，
并给出**可核验**的对照解读。

> 当前状态：**v0（M0–M4）+ v1 三阶段全部完成并验收** —— 36+162+174+39+48+43+42 = **544 项断言**全部离线可复现：
> `cd backend && ./scripts/run_all_checks.sh`（含前端构建与生产页面渲染、gold set 评估）。
>
> v1 已做：**双栏对照阅读器**（左栏可切「PDF 原版（真实排版，滚动看全文）」与「原文文本（可划选）」，
> 右栏代码各自独立滚动、引用高亮）、解释全字段渲染、
> 解释质量门槛（只有一句话的解释会被打回重写）、**在原文里划选 → 加为定位目标**、
> **追问对话**（Agent 自己去翻论文和代码；每条消息 8 次工具调用上限；回答里的代码位置逐个机械核对）。
> 网页上能跑通完整流程：上传论文 → 侦察出创新点清单 → 勾选 → 克隆仓库定位 → **双栏对照 + 点开看原文 + 覆盖率/核验率**。
> 详细规格见 [`docs/v0-spec.md`](docs/v0-spec.md)，进度见该文档 §12 里程碑表。

## 已经能做什么

```
上传 PDF ─► 阶段 A（侦察）：Agent 自主决定读哪几页 ─► 3-5 条核心创新点
                                              ├─ 每条带页码 + 原文引用
                                              ├─ 引用被后端逐条机械核验（编的会被打回重做）
                                              └─ 带 search_hints，供阶段 B 到代码里搜索
        ─► 你在页面上勾选想深入哪几条 ─► 阶段 B（定位）：浅克隆仓库 → Agent 自主探索
                                              ├─ 每条结论带 path + 行号区间 + why
                                              ├─ snippet_sha256 由后端从 git 对象里算出来
                                              ├─ verify.py 重放核验 → citation_verifiable_rate
                                              └─ 找不到就写 not_found（附搜过什么）
```

## 这个项目的两条底线

1. **引用必须可机械核验**：每条代码引用都带 `commit_sha / path / 行号区间 / 片段 hash`，
   后端会重放校验（`verify.py`）。头号指标是 `citation_verifiable_rate`，不是"读起来像不像"。
2. **找不到就说找不到**：`not_found` 是合格输出，不是失败。

## 评估数字

这一节是**这个项目最该被认真看的地方**：一个"看起来很懂"的解读很容易做，难的是知道它有多准。

| 指标 | mock（脚本化理想 Agent） |
|---|---|
| 创新点清单召回（plan recall） | 100% |
| 创新点清单准确（plan precision） | 100% |
| 文件级召回（file recall） | 100% |
| 文件级准确（file precision） | 100% |
| 符号级召回（symbol recall） | 100% |
| **引用可核验率** | 100% |
| 诚实报「未找到」 | 100% |
| 引用总数 / 通过核验 | 2/2 |
| 平均工具调用 / 总用时 | 7 次 / 1.4s |

> ⚠️ 这一行是**离线模式**：模型行为由 `devtools/mock_provider.py` 脚本化，用来验证评估链路与指标定义本身，**不代表真实模型的能力**。
> 真实数字请用 `--provider real` 跑你自己的模型。


**怎么读这张表**：`file_recall` 问的是"论文的创新点，它有没有在正确的文件里找到"；
`citation_verifiable_rate` 问的是"它给出的每一条代码引用，是不是真的能在那个 commit 的那些行里找到"
（由后端从 git 对象重放核验，不依赖任何模型判断）；"诚实报未找到"问的是"仓库里确实没有实现时，它会不会编一个出来"。

### 标注纪律（比数字本身更重要）

gold set 是一张**标准答案表**（`backend/eval/goldset.yaml`）。要让上面的数字有意义，必须守住三条：

1. **先标注、后运行**：人工读完论文和代码、写下 `must_find_files` 并**冻结 repo 的 commit**，之后才允许跑系统。
   先跑系统再回头补标注 = 数据污染，数字会骗自己。
2. **commit 必须冻结**：仓库每天都在变，不钉死 commit 的指标没有意义。
   `m4_check` 会检查冻结的 commit 是否还在，不一致就报错提醒你重新标注。
3. **gold set 只看一次**：想边跑边改 prompt，请另开"调参集"；gold set 封存到出报告那一刻。

### 你自己跑一遍

```bash
cd backend
# 离线：验证评估链路本身（不花钱，用脚本化的"理想 Agent"）
.venv/bin/python -m eval.run_eval

# 真实：用你自己的模型 + 真实论文与仓库
PAPERLENS_EVAL_BASE_URL=https://api.deepseek.com/v1 \
PAPERLENS_EVAL_API_KEY=sk-xxx \
PAPERLENS_EVAL_MODEL=deepseek-chat \
.venv/bin/python -m eval.run_eval --provider real --label "DeepSeek-V3"
```

真实论文按 `goldset.yaml` 里的模板加（放到 `backend/eval/pdfs/`），然后把 `--provider real` 跑出来的
表格贴到这一节。**离线那一行永远只是基线，不要拿它当项目效果。**

## 环境要求

- **Python 3.11+**（必须。`litellm` 用了 3.11 才有的 `typing.NotRequired`，3.10 会直接 ImportError）
- Node.js 20+（前端，M3 开始用）

本机没有 3.11+ 的话，用 [uv](https://docs.astral.sh/uv/) 装一个（推荐，不需要 sudo）：

```bash
pip install uv          # 或 curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12
```

## 快速开始

### 后端

```bash
cd backend

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 离线验收：不需要任何真实 API key，会自己拉起 mock provider 跑完整链路
.venv/bin/python -m scripts.m0_check   # M0 回归（36 项）
.venv/bin/python -m scripts.m1_check   # M1 阶段 A（162 项）
.venv/bin/python -m scripts.m2_check   # M2 阶段 B（174 项）
.venv/bin/python -m scripts.m3_check   # M3 对照界面 + 前端构建渲染（39 项；跑前先停掉 next dev）
.venv/bin/python -m scripts.m4_check   # M4 gold set + 指标自检 + README 一致性（48 项）
.venv/bin/python -m scripts.m6_check   # v1② 划选→定位目标（43 项；跑前先停掉 next dev）
.venv/bin/python -m scripts.m7_check   # v1③ 追问对话（42 项；跑前先停掉 next dev）

# 或者一键全跑（推荐）
./scripts/run_all_checks.sh

# 启动服务
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

打开 http://127.0.0.1:8000/docs 可以直接在浏览器里试接口。

### 前端

```bash
cd frontend
pnpm install
pnpm dev            # http://localhost:3000
```

前端默认连 `http://127.0.0.1:8000`，要改就复制 `.env.local.example` 成 `.env.local`。

### 零成本试用（推荐第一次这样跑）

不用任何真实 API key：先起一个本地假端点，它会扮演一个"听话的模型"（读论文 → 交清单），
把你真正想验证的东西（工具调用、事件流、引文核验、UI）全部跑一遍。

```bash
cd backend
.venv/bin/python -m uvicorn devtools.mock_provider:app --port 8123
```

然后在网页上填：`base_url = http://127.0.0.1:8123/v1`、`api_key = mock-key`、`model = mock-model`
→ 点「运行自检」→ 上传 `backend/tests/fixtures/synthetic_paper.pdf` → 开始侦察 → 勾选创新点
→ 仓库地址填 `backend/tests/fixtures/sample_repo` 的**绝对路径**（需要后端带
`PAPERLENS_ALLOW_LOCAL_REPO_PATHS=true` 启动）→ 开始定位，看完整的引用核验）。

没有那个开关时，仓库地址必须是 https 的 github.com / gitlab.com 地址。

想看真实效果就把 provider 换成你自己的（DeepSeek / OpenAI / Anthropic / 本地 Ollama 都行），
上传一篇真论文。**先跑自检**：它会替你把"这个模型到底能不能用"这件事判定掉。

### 自带 key（BYOK）

不写死任何服务商。前端/请求里传这三个字段即可，支持任何 OpenAI 兼容端点
（OpenAI、DeepSeek、Moonshot、Groq、OpenRouter、vLLM、Ollama、one-api 网关…）以及 Anthropic：

```json
{
  "protocol": "openai-compatible",
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "sk-...",
  "model": "deepseek-chat"
}
```

**上手前先跑自检**，它会替你判断这个模型能不能用：

```bash
curl -s http://127.0.0.1:8000/api/provider/smoke-test \
  -H 'content-type: application/json' \
  -d '{"protocol":"openai-compatible","base_url":"https://api.deepseek.com/v1","api_key":"sk-...","model":"deepseek-chat"}'
```

自检会给出人话结论，例如"该端点没有返回工具调用（tool_calls）……无法在这样的模型上工作"。
**本项目完全依赖可靠的 function calling**，不支持工具调用的模型必须在这里被拦住，而不是产出垃圾。

> 兼容性备注：个别套了 Cloudflare 的第三方网关会**按 User-Agent 把 Python SDK 的请求当 bot 直接 403**
> （2026-09-13 实测某中转站：同 key 同请求体，SDK UA → 403 HTML，自定义 UA → 正常）。
> PaperLens 的 LLM 请求默认带自定义 UA `PaperLens/0.1` 来避开；若你的网关仍拦，设
> `PAPERLENS_HTTP_USER_AGENT=任一浏览器UA` 后重启后端再自检。自检失败时会把这个开关写进诊断提示。

### 接口一览（M0）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 + 当前预算上限 |
| POST | `/api/provider/smoke-test` | 启动自检（两轮工具调用，判定模型能不能用） |
| POST | `/api/runs` | multipart 上传 PDF + provider 配置，返回 `run_id` |
| POST | `/api/runs/{id}/recon` | 启动阶段 A（侦察），产出创新点清单 |
| POST | `/api/runs/{id}/locate` | 阶段 B：对勾选的创新点做代码定位 + 引用核验 |
| GET/POST/PATCH/DELETE | `/api/runs/{id}/plan/items…` | 用户自己指定/编辑定位目标（在原文里划选） |
| POST | `/api/runs/{id}/chat` | 追问一句（它会自己决定要不要去查代码/论文） |
| GET | `/api/runs/{id}/chat` | 取回对话记录（刷新页面恢复） |
| GET | `/api/runs/{id}/events` | 所有阶段的统一 SSE 事件流（`Last-Event-ID` 断线重连） |
| GET | `/api/runs/{id}` | 事件历史与产物（刷新页面回放） |
| GET | `/api/runs/{id}/file` | 读某个 commit 上的一段代码（点开引用看原文） |
| GET | `/api/runs/{id}/paper/page/{n}` | 读论文某一页原文（点开论文证据） |
| POST | `/api/runs/{id}/cancel` | 取消运行 |

## 隐私与安全

- 你的 `api_key` **只在请求体里出现**：不落库、不写日志、不进错误堆栈（`ProviderConfig` 里已标记 `repr=False`）。
- 论文与代码内容一律当作**不可信数据**：其中出现的任何"指令"都不生效（prompt injection 防线）。
- 克隆用户提供的仓库已按 §10 硬化：https + 域名白名单（github.com / gitlab.com，可加别的托管站）
  + 地址判定（拒绝回环/链路本地等 SSRF 靶子；挂代理/内网镜像的用户按 §10 的三层判据放行）
  + 浅克隆并禁 hooks/submodule/LFS + 体积与超时上限 + **绝不执行仓库里的任何代码**。
- 引用不是模型说了算：`snippet_sha256` 由后端从 `git show` 的对象里算，事后改代码骗不过核验。

### 仓库地址的判定（报「解析到了内网地址」时先看这里）

守卫不再拿"本机 DNS 的答案"一票否决：挂 TUN + fake-ip 代理（Clash/Mihomo/sing-box/Surge）或
公司分流 DNS 时，本机解析到的地址只是**代理的占位地址**，不是目的地。默认 `local` 模式
（单机自用）只拒绝回环/链路本地/组播这类绝不可能是托管站的地址，其它非公网解析结果
**只提示不阻断**（提示会显示在时间线上）。

| 你的情况 | 怎么办 |
|---|---|
| 挂 Clash/Mihomo 等 TUN 代理，提示解析到 `198.18.x.x` | 默认已放行，无需配置 |
| 公司内网 GitLab / 内网镜像 | 白名单加域名 `PAPERLENS_REPO_ALLOWED_HOSTS=github.com,gitlab.com,git.corp`（非 443 写 `git.corp:8443`）；解析到内网的再用 `PAPERLENS_REPO_ALLOW_CIDRS=10.20.0.0/16` 显式信任 |
| 用 Gitee / Bitbucket / GitHub Enterprise | 同上，把这些域名加进 `PAPERLENS_REPO_ALLOWED_HOSTS` |
| 企业代理要求 git 走代理 / 自签 CA | `local` 模式默认**继承**你的 `~/.gitconfig`（`http.proxy` / `http.sslCAInfo` 都生效） |
| 公网多租户部署 | 设 `PAPERLENS_REPO_NETWORK_MODE=hosted`（严格判定 + 公网 DoH 交叉核验），并**另外做网络层隔离**：进程内的 DNS 检查不是安全边界 |
| 仓库里有大体积演示视频/数据集，不想全下载 | 默认就只取**源码视图**（部分克隆 + 稀疏检出）：实测某个含 83MB 演示视频的仓库从 237MB 降到 2.3MB。想调整规则用 `PAPERLENS_REPO_SPARSE_EXCLUDE=!*.bin,!/docs/assets`；想彻底关掉用 `PAPERLENS_REPO_SPARSE=false` |
| 提示"另有 N 个较大的二进制文件被下载了但工具不会读" | 照提示里给的 `PAPERLENS_REPO_SPARSE_EXCLUDE=!*.xxx` 加上那条规则即可（冷门二进制格式没法预判，所以系统会点名） |
| 结束原因是「工具调用次数达到上限 40」而**一条结论都没提交** | 以前模型不知道自己在烧最后一次机会（实测有一轮 40 次调用、0 条结论）。现在**边搜边交**：提示词要求证据够了就提交，并在用到 60% / 85% / 墙钟告急时注入「[预算提醒] 已用 24/40…立刻提交已确认的结论」，时间线上同样可见。想直接放宽：`PAPERLENS_MAX_TOOL_CALLS=80`（配 `PAPERLENS_WALL_CLOCK_SECONDS`）——但更该先解决的是「没提交」，不是「次数不够」 |
| 跑着跑着报 `InternalServerError: Connection error.` 或 502/503 | 这是**瞬时故障**（网络抖动 / 代理不稳 / 中途站过载），现在会**自动退避重试**（5s 起跳），时间线显示「连接中断…重试中」。若重试后仍失败：重跑一次通常就好，反复出现就检查本机代理或换端点。**已定位并核验过的结论照常交付**，不会白跑 |
| 跑着跑着报 `RateLimitError`（网关限流，如"1 分钟最多 10 次"） | 通常**不用配**：端点在响应头里声明了限额（`x-ratelimit-limit-requests: 10, 10;w=60`）就自动按那个节奏走；只说话术的也认（中英文都行）。想看当前生效的节奏：`GET /api/health` 的 `llm_pacing`，或跑一次自检（结论里会写「端点限额已识别（来自响应头）——每 6.0s 一次调用」）。要手动钉住就写 `PAPERLENS_LLM_MAX_REQUESTS_PER_MINUTE=10` 再重启后端。**撞到 429 会自动退避重试**（时间线显示「端点限流，等待 Ns 后重试」）；**额度用尽/欠费不会重试**（直接告诉你重试无用）；**TPM（token/分钟）**会按 token 节流并建议减少上下文。**已定位到的结论不会丢**——失败也会交付并核验已确认的部分 |

环境变量用逗号分隔或 JSON 都行（`PAPERLENS_REPO_ALLOWED_HOSTS=a,b` 是合法的）；
改完**必须重启后端** —— 配置在导入时读取。

## 目录结构

```
backend/app/providers.py           BYOK 唯一抽象层（协议差异只允许存在于此）+ 启动自检
backend/app/agent/loop.py          Agent 工具循环（目标保持 ~200 行）
backend/app/paper.py               PDF 读取层 + 引文核验（不含任何"理解"）
backend/app/prompts.py             提示词（含不可信内容声明）
backend/app/events.py              事件总线 + SSE（事件契约的实现）
backend/app/main.py                FastAPI 端点
backend/app/agent/tools/base.py    工具基类 + 截断 + 路径白名单
backend/app/agent/tools/paper_tools.py   list_pages / get_page_text / search_paper / read_paper_all
backend/app/repo_source.py         仓库访问层（§10 硬化全部在这里）
backend/app/verify.py              确定性重放核验（citation_verifiable_rate 的来源）
backend/app/agent/tools/repo_tools.py    repo_tree / search_code / read_file
backend/app/agent/tools/findings.py      record_plan（阶段 A）+ record_finding（阶段 B）
backend/devtools/mock_provider.py  本地假端点（离线验收 / 零成本试用）
backend/scripts/harness.py         验收脚本公用件
backend/scripts/m1_check.py        M1 验收脚本（80 项断言）
backend/scripts/m2_check.py        M2 验收脚本（174 项断言）
backend/tests/repo_fixture.py      合成测试仓库（内容自洽：有低秩实现、没有冻结主干）
backend/tests/paper_fixture.py     合成论文 PDF（测试数据可复现）
frontend/app/page.tsx              主页面：provider 表单 / 上传 / 仓库 / 清单 / 时间线 / 对照
frontend/components/ComparePanel.tsx   论文证据 ↔ 代码引用的双栏对照
frontend/components/TextViewer.tsx     点开看原文（代码按行高亮 / 论文整页）
frontend/components/CoverageCard.tsx   覆盖率与预算
frontend/components/Reader.tsx         双栏阅读器（左原文/右代码，划选可加为定位目标）
backend/app/plan.py                    用户自定义定位目标（清单的可编辑状态）
backend/app/chat.py                    追问对话（历史落盘 + 回答里的代码位置机械核对）
frontend/components/ChatPanel.tsx      追问面板（显示"这次查了什么"与引用核验徽章）
backend/scripts/dev_services.sh        一键起停三个开发服务（前端/后端/假端点）
docs/v0-spec.md                    v0 规格（决策 / 范围 / schema / 安全 / 评估）
```

## 开发约定

- 依赖锁精确版本；升级依赖必须单独一次提交，并重跑 `m0_check` 和 gold set。
- 工具必须能截断、必须有路径白名单、必须抛异常而不是把错误当内容返回。
- 前端只能展示"可观测行动轨迹"（工具名、参数、结果摘要、阶段结论）——**不展示思维链**，
  因为 OpenAI/Anthropic 的 API 不返回可展示的原始推理过程。
