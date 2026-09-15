"""全局配置：预算护栏默认值 + 数据目录。

所有值都可以用环境变量覆盖，前缀 PAPERLENS_，例如：
    PAPERLENS_MAX_TOOL_CALLS=20
    PAPERLENS_WALL_CLOCK_SECONDS=300

这些默认值对应 docs/v0-spec.md §8「运行治理」。
"""

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _as_list(value: Any) -> Any:
    """list 型配置的容错解析：JSON（`["a","b"]`）和逗号分隔（`a,b`）都接受。

    为什么需要：pydantic-settings 对 list 字段默认只认 JSON，用户按直觉写
    `PAPERLENS_REPO_ALLOWED_HOSTS=github.com,gitee.com` 会让**整个后端在导入时崩掉**
    （SettingsError: error parsing value for field ...，2026-09-14 实测）。
    配置项的报错文案全都引导用户去设这些变量，所以这层容错是必须的。
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass  # 落到逗号分隔
        return [item.strip() for item in text.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PAPERLENS_", env_file=".env", extra="ignore"
    )

    # 数据目录：每次分析一个子目录 data/<run_id>/
    data_dir: Path = Path("data")

    # ---- §8 预算护栏（超限不是崩溃，而是"用已确认的部分交付"）----
    max_tool_calls: int = 40
    max_input_tokens: int = 1_500_000
    wall_clock_seconds: int = 600

    # ---- 追问对话：每条消息的预算（问一句不该烧掉整个 run 的额度）----
    chat_max_tool_calls: int = 8
    chat_max_input_tokens: int = 400_000
    chat_wall_clock_seconds: int = 180

    # ---- 单次 LLM 调用的上限，防止某个端点卡死 ----
    per_turn_timeout_seconds: int = 180

    # ---- LLM 限流（§8 运行治理，2026-09-15 补）----
    # 一次定位/侦察要几十次 LLM 调用（一次 turn 一次请求），而有些网关卡得很死：
    # 实测某中转站"1 分钟内最多请求 10 次"，20 轮只花 61.6s ≈ 19.5 次/分钟 → 必然撞限流。
    #   0 = 不主动节流（只在撞到 429 后退避重试，并记住这次的节奏）；
    #   N = 客户端按每分钟 N 次发请求（宁可慢，也别半路失败）。
    # 你知道自己端点的限额就写上（例：10），不知道就留 0，代码会在第一次 429 后自适应。
    llm_max_requests_per_minute: int = 0
    # 撞到 429 最多重试几次（退避：优先用响应里的 retry-after，其次指数退避）
    llm_max_retries: int = 5

    # ---- LLM 请求的 User-Agent ----
    # 为什么可配：有些套了 Cloudflare 的第三方网关会把 openai-python SDK 的默认 UA
    # （"OpenAI/Python x.x"）当 bot 直接 403（实测于 2026-09-13，1li.li），
    # 请求根本到不了模型。默认带一个自定义标识（实测可通过），官方端点对 UA 无感。
    # 设为空字符串则回退为 SDK 默认 UA。
    http_user_agent: str = "PaperLens/0.1"

    # ---- 产物版本（改了提示词就要改它：幂等键里会用到）----
    prompt_version: str = "v0.2-m2"

    # ---- 上传限制（§10）----
    max_upload_mb: int = 50
    max_pages: int = 50

    # ---- 克隆策略：只取"源码视图"（2026-09-14）----
    # 为什么要这样：整条链路只读源码文本，但原来的 --depth 1 全量检出会把
    # 演示视频、数据集、以及 .git 里那份压缩拷贝一起拉下来。实测 zju3dv/INTACT-JEPA：
    # 为了读 121 个文本文件（1.8MB）下载了 237MB（工作区 140MB 里 6 个演示视频占 83MB，
    # .git 97MB）；改成部分克隆 + 稀疏检出后是 2.5MB / 2 秒，源码一个不少。
    # 排除规则直接复用 SKIP_DIRS / BINARY_SUFFIXES —— "不下载什么"与"工具不读什么"
    # 必须是同一套标准，否则就会出现"按一套标准跳过、按另一套标准卡体积上限"。
    repo_sparse: bool = True
    # 追加的稀疏排除规则（gitignore 风格的负向模式，逗号分隔或 JSON）
    # 例：PAPERLENS_REPO_SPARSE_EXCLUDE=!data,!/docs/assets
    repo_sparse_exclude: Annotated[list[str], NoDecode] = []

    # ---- 仓库克隆硬化（§10）----
    # 只允许这些域名（https），避免"用户填什么我们就去连什么"变成 SSRF 跳板。
    # 支持 `host` 或 `host:port`（自建 GitLab 常用 8443）；写成逗号分隔或 JSON 都行。
    repo_allowed_hosts: Annotated[list[str], NoDecode] = ["github.com", "gitlab.com"]
    # 克隆超时：60s 对本地/内网够用，但真实 GitHub（尤其经本机代理）实测 7MB 的
    # 小仓库也常常超过 60s（预检 0.8s、下载 2 分钟未完，2026-09-13 实测
    # zju3dv/INTACT-JEPA），所以默认放宽到 300s。网络更差可再用环境变量调大。
    repo_clone_timeout_seconds: int = 300
    repo_max_mb: int = 200
    # 只给测试用：允许克隆本地目录（默认关闭，公网部署时绝不能打开）
    allow_local_repo_paths: bool = False
    # 逃生开关：放行**所有**非公网解析结果（全有全无）。默认关闭。
    # 2026-09-14 起优先用下面的 repo_network_mode / repo_allow_cidrs：这个开关粒度太粗。
    repo_allow_private_ips: bool = False

    # ---- SSRF 守卫的严格度：跟部署模式绑定（2026-09-14 重做）----
    # 背景：守卫原来拿"本机 DNS 的答案"一票否决，但挂了 TUN/fake-ip 代理
    # （Clash/Mihomo/sing-box/Surge）或企业分流 DNS 时，本机解析出来的地址根本不是
    # 目的地——代理答一个占位地址，真正的目的地由代理按域名决定。实测同一类使用者的
    # 命运取决于代理厂商把段设成什么（198.18/15 被拦、28/8 侥幸放过、240/4 被拦、
    # NAT64 的 64:ff9b::/96 因 is_reserved 也被拦）。
    #   local （默认，单人自用）：用户就是机器主人，"防自己"没有意义，非公网解析结果
    #          只记一条警告、不阻断；只有回环/链路本地/组播/未指定这类"绝不可能是
    #          代码托管站"的地址仍然拒绝。
    #   hosted（公网多租户）：非公网解析结果按拒绝处理（除非 DoH 核验为代理占位符，
    #          或部署者用 repo_allow_cidrs 显式信任）。注意：进程内的 DNS 检查不是
    #          安全边界（rebinding 可绕），公网部署必须另有网络层隔离。
    repo_network_mode: Literal["local", "hosted"] = "local"
    # 显式信任的地址段（逗号分隔）：企业内网镜像、本机代理的 fake-ip 段等。
    # 例：PAPERLENS_REPO_ALLOW_CIDRS=198.18.0.0/15,10.20.0.0/16
    repo_allow_cidrs: Annotated[list[str], NoDecode] = []
    # git 全局/系统配置是否隔离。None = 跟随部署模式（local 继承，hosted 隔离）。
    # 为什么要继承：企业用户靠 `git config --global` 配 http.proxy / http.sslCAInfo
    # （MITM 代理的 CA）/ insteadOf（内网镜像），隔离掉他们**永远克隆不通**，
    # 而错误信息完全指不到真因（2026-09-14 实测：GIT_CONFIG_GLOBAL=/dev/null 下
    # `git config --global --get http.proxy` 读不到）。
    repo_isolate_git_config: bool | None = None
    # 非公网解析结果是否用公网 DoH 交叉核验（区分"代理占位符"和"真内网目标"）。
    # auto = 只在 hosted 模式下核验（local 模式本来就不阻断，不需要外呼）。
    repo_dns_crosscheck: Literal["auto", "on", "off"] = "auto"
    # 核验端点必须是 literal IP（否则又要靠本机 DNS，等于循环依赖）
    repo_doh_endpoints: Annotated[list[str], NoDecode] = [
        "https://1.1.1.1/dns-query",
        "https://8.8.8.8/resolve",
    ]
    repo_doh_timeout_seconds: float = 3.0

    @field_validator(
        "repo_allowed_hosts",
        "repo_allow_cidrs",
        "repo_doh_endpoints",
        "repo_sparse_exclude",
        "cors_origins",
        mode="before",
    )
    @classmethod
    def _list_from_env(cls, value: Any) -> Any:
        return _as_list(value)

    @property
    def isolate_git_config(self) -> bool:
        """生效值：显式配置优先，否则 hosted 隔离、local 继承。"""
        if self.repo_isolate_git_config is not None:
            return self.repo_isolate_git_config
        return self.repo_network_mode == "hosted"

    @property
    def dns_crosscheck_enabled(self) -> bool:
        if self.repo_dns_crosscheck == "on":
            return True
        if self.repo_dns_crosscheck == "off":
            return False
        return self.repo_network_mode == "hosted"

    # ---- 前端本地开发的来源白名单 ----
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


settings = Settings()
