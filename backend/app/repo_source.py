"""仓库访问层。

docs/v0-spec.md §10 的硬化全部集中在这个文件里，其它地方不许直接调 git 或读仓库文件。

要防的四件事：
1. **SSRF**：用户填的 URL 就是我们要去连的地址。只允许 https + 白名单域名，
   并且解析后的 IP 不能是内网/回环/链路本地/保留地址。
2. **恶意仓库**：禁 hooks、禁 submodule、禁 LFS、不跑任何仓库里的代码。
3. **资源耗尽**：克隆超时 + 体积上限，搜索有文件数与时间上限。
4. **路径穿越**：所有路径参数必须落在本次 run 的仓库目录内。

另外一条不那么显然的：**我们只读文件，从不执行**。
不 pip install、不 import、不跑 setup.py / Makefile —— 这是"读别人的代码"和"跑别人的代码"的分界线。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import selectors
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import settings

# 这些目录扫过去只会浪费时间（而且是别人的依赖，不是论文的实现）
SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".env",
    "dist",
    "build",
    ".next",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
    ".idea",
    ".vscode",
    "third_party",
    "thirdparty",
}

# 二进制/权重文件：不读、不搜索、**也不下载**（稀疏检出直接跳过）
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz", ".svg",
    ".tif", ".tiff", ".heic", ".avif", ".psd", ".ai", ".eps",
    ".pt", ".pth", ".ckpt", ".bin", ".onnx", ".safetensors", ".npy", ".npz", ".pkl", ".pickle",
    ".i16", ".f32", ".f64", ".raw", ".pcm", ".ppm", ".pgm",
    ".so", ".dylib", ".dll", ".a", ".o", ".obj", ".exe", ".apk", ".ipa", ".dmg", ".iso", ".img",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".whl", ".jar",
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".webm", ".mkv", ".flv", ".wmv", ".m4v",
    ".aac", ".flac", ".ogg", ".opus",
    ".glb", ".gltf", ".fbx", ".blend", ".stl", ".ply",
    ".parquet", ".arrow", ".h5", ".hdf5", ".db", ".sqlite", ".sqlite3",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
}

MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024       # 单文件读取上限
MAX_SEARCH_FILES = 4000                      # 一次搜索最多扫多少文件
MAX_SEARCH_SECONDS = 8.0                     # 一次搜索的时间预算


class RepoError(Exception):
    """可预期的仓库访问失败（会被翻译成人话回给用户/模型）。"""


# ---------------------------------------------------------------------------
# URL 校验 + 地址判定（SSRF 守卫）
# ---------------------------------------------------------------------------
# 判据为什么不能是"本机 DNS 的答案一票否决"（2026-09-14 重做）：
# 挂了 TUN + fake-ip 的代理（Clash/Mihomo/sing-box/Surge）或企业分流 DNS 时，本机解析
# 出来的地址**不是目的地**——代理按域名决定去向，本机 DNS 只答一个占位地址。
# 实测（2026-09-14）：github.com → 198.18.0.47（占位），公网 DoH 说 140.82.113.3，
# `git ls-remote` 与真克隆都正常。而"猜段"不泛化：198.18/15 被拦、28/8 侥幸放过、
# 240/4 被拦、NAT64 的 64:ff9b::/96 因 is_reserved 也被拦——同类使用者的命运取决于
# 代理厂商设了什么段。所以判定改成分层：
#   1. 部署模式决定严格度：local（默认，单人自用）只拦"绝不可能是代码托管站"的地址；
#      hosted（公网多租户）才把非公网解析结果当拒绝项；
#   2. 需要判定时，用公网 DoH 交叉核验"域名在公网的真实归属"（与厂商/段/IP 族无关）；
#   3. 部署者可以用 PAPERLENS_REPO_ALLOW_CIDRS 显式信任某个网段（内网镜像、fake-ip 段）。
KIND_CN = {
    "loopback": "回环",
    "link_local": "链路本地",
    "multicast": "组播",
    "unspecified": "未指定",
    "private": "私有",
    "reserved": "保留",
    "non_public": "非公网",
    "public": "公网",
}

# 绝不可能是代码托管站的地址类别：回环/链路本地/组播/未指定。
# 云元数据（169.254.169.254）就在链路本地段里，是 SSRF 的头号靶子。
HARD_BLOCK_CATEGORY = "hard"


@dataclass(slots=True)
class AddressFinding:
    """一条解析结果的分类。"""

    address: str
    kind: str       # loopback / link_local / multicast / unspecified / private / reserved / non_public / public
    category: str   # hard / non_public / public

    def label(self) -> str:
        return f"{self.address}（{KIND_CN.get(self.kind, self.kind)}）"


def _classify_address(ip: Any) -> tuple[str, str]:
    """把 IP 分成 (kind, category)。category 才是判定依据。"""
    if ip.is_loopback:
        return "loopback", HARD_BLOCK_CATEGORY
    if ip.is_link_local:
        return "link_local", HARD_BLOCK_CATEGORY
    if ip.is_multicast:
        return "multicast", HARD_BLOCK_CATEGORY
    if ip.is_unspecified:
        return "unspecified", HARD_BLOCK_CATEGORY
    # 真正的公网地址：is_global 且不是保留段（NAT64 的 64:ff9b::/96 是 is_global
    # 但 is_reserved，属于运营商侧，不算"公网目的地"）
    if ip.is_global and not ip.is_reserved:
        return "public", "public"
    if ip.is_private:
        return "private", "non_public"
    if ip.is_reserved:
        return "reserved", "non_public"
    return "non_public", "non_public"


def resolve_host_addresses(host: str) -> list[AddressFinding]:
    """解析域名并分类。**测试会 monkeypatch 这个函数**，所以判定逻辑本身不碰网络。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise RepoError(f"域名解析失败：{host}") from exc
    findings: list[AddressFinding] = []
    seen: set[str] = set()
    for info in infos:
        address = info[4][0]
        if address in seen:
            continue
        seen.add(address)
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        kind, category = _classify_address(ip)
        findings.append(AddressFinding(address=address, kind=kind, category=category))
    return findings


def _allowed_host_ports() -> dict[str, set[int]]:
    """白名单解析成 {host: {port}}。条目可以写成 `host` 或 `host:port`（自建 GitLab 常用 8443）。"""
    allowed: dict[str, set[int]] = {}
    for raw in settings.repo_allowed_hosts:
        entry = str(raw).strip().lower()
        if not entry:
            continue
        host, port_text = entry, ""
        if entry.startswith("["):                      # [::1]:8443 形式
            host, _, rest = entry.partition("]")
            host = host[1:]
            port_text = rest.lstrip(":")
        else:
            head, sep, tail = entry.rpartition(":")
            if sep and tail.isdigit():
                host, port_text = head, tail
        if port_text.isdigit():
            allowed.setdefault(host, set()).add(int(port_text))
        else:
            allowed.setdefault(host, set()).add(443)
    return allowed


def _trusted_networks() -> list[Any]:
    """PAPERLENS_REPO_ALLOW_CIDRS 解析。写错了要给看得懂的报错，而不是崩。"""
    networks: list[Any] = []
    for raw in settings.repo_allow_cidrs:
        text = str(raw).strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise RepoError(
                f"PAPERLENS_REPO_ALLOW_CIDRS 里的网段看不懂：{text}（例：198.18.0.0/15 或 10.20.0.0/16）"
            ) from exc
    return networks


def _address_trusted(address: str, networks: list[Any]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks if network.version == ip.version)


def _proxy_env_var() -> str | None:
    """环境里配了正向代理？配了的话域名由代理解析，本机 DNS 答案不代表目的地。"""
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(name):
            return name
    return None


# ---- 公网 DoH 交叉核验 ------------------------------------------------------
# 端点必须是 literal IP：用域名去查 DNS 等于循环依赖。
_DOH_CACHE: dict[str, tuple[float, bool | None]] = {}
_DOH_CACHE_TTL_SECONDS = 300.0


def _doh_verdict_from_payload(payload: Any) -> bool | None:
    """DoH JSON → True=公网有 A 记录 / False=公网 DNS 也没有公网 A / None=响应看不懂。"""
    if not isinstance(payload, dict):
        return None
    answers = payload.get("Answer")
    if isinstance(answers, list):
        saw_public = False
        saw_other = False
        for item in answers:
            if not isinstance(item, dict) or item.get("type") != 1:
                continue
            try:
                ip = ipaddress.ip_address(str(item.get("data")))
            except ValueError:
                continue
            if _classify_address(ip)[1] == "public":
                saw_public = True
            else:
                saw_other = True
        if saw_public:
            return True
        if saw_other:
            return False
        return False   # 有 Answer 但没有 A 记录（只有 CNAME 等）→ 公网没有公网 A
    if "Status" in payload:
        return False   # NXDOMAIN / SERVFAIL 之类：格式合法，但公网没有公网 A
    return None


def _doh_public_verdict(host: str) -> bool | None:
    """用公网 DoH 核验域名归属。返回 True/False/None（None=核验不了）。"""
    now = time.monotonic()
    hit = _DOH_CACHE.get(host)
    if hit and hit[0] > now:
        return hit[1]
    verdict: bool | None = None
    for endpoint in settings.repo_doh_endpoints:
        try:
            with httpx.Client(timeout=settings.repo_doh_timeout_seconds) as client:
                if endpoint.rstrip("/").endswith("/dns-query"):
                    response = client.get(
                        endpoint,
                        params={"name": host, "type": "A"},
                        headers={"accept": "application/dns-json"},
                    )
                else:
                    response = client.get(endpoint, params={"name": host, "type": "A"})
                verdict = _doh_verdict_from_payload(response.json())
        except Exception:  # noqa: BLE001 —— 核验端点不可达绝不能弄死主流程
            continue
        if verdict is not None:
            break
    if len(_DOH_CACHE) > 256:      # 无界缓存的护栏：纯内存、无敏感性
        _DOH_CACHE.clear()
    _DOH_CACHE[host] = (now + _DOH_CACHE_TTL_SECONDS, verdict)
    return verdict


def _judge_resolved_addresses(host: str, findings: list[AddressFinding]) -> list[str]:
    """按部署模式判定解析结果：返回要提示用户的 note，该拒绝时抛 RepoError。"""
    notes: list[str] = []
    if not findings:
        return notes
    resolved = "、".join(f.address for f in findings)
    if settings.repo_allow_private_ips:
        return [
            f"已开启 PAPERLENS_REPO_ALLOW_PRIVATE_IPS（全有全无的逃生开关）：跳过地址判定，"
            f"{host} 解析到 {resolved}"
        ]

    networks = _trusted_networks()
    trusted = [f for f in findings if _address_trusted(f.address, networks)]
    untrusted = [f for f in findings if not _address_trusted(f.address, networks)]
    if not untrusted:
        notes.append(
            f"{host} 解析到的地址（{resolved}）在 PAPERLENS_REPO_ALLOW_CIDRS 显式信任范围内，放行"
        )
        return notes

    hard = [f for f in untrusted if f.category == HARD_BLOCK_CATEGORY]
    non_public = [f for f in untrusted if f.category == "non_public"]
    public = [f for f in untrusted if f.category == "public"]

    def _hard_error() -> RepoError:
        detail = "、".join(f.label() for f in hard)
        return RepoError(
            f"{host} 解析到了内网地址：{detail}。回环/链路本地/组播这类地址不可能是代码托管站，"
            "而且是 SSRF 的经典靶子（云元数据 169.254.169.254 就在链路本地段里），默认拒绝。"
            "若你的环境确实如此（本机镜像、企业代理解析），用 PAPERLENS_REPO_ALLOW_CIDRS=<该网段> "
            "显式放行，或设 PAPERLENS_REPO_ALLOW_PRIVATE_IPS=true 放行所有非公网地址。"
        )

    if hard:
        raise _hard_error()
    if not non_public:
        return notes   # 全是公网地址，正常放行

    detail = "、".join(f.label() for f in non_public)
    mixed = f"（同一域名还解析出公网地址 { '、'.join(f.address for f in public) }）" if public else ""

    if settings.repo_network_mode == "local":
        # local：用户就是机器主人，"防自己"没有意义；非公网解析结果最常见的原因恰恰是
        # 用户自己的代理（TUN+fake-ip）或企业分流 DNS。只提示，不阻断。
        notes.append(
            f"注意：{host} 在本机解析到非公网地址 {detail}{mixed}。当前是 local 模式，按"
            "「代理占位/分流 DNS」放行；如果这不是你自己的代理或镜像，请先确认本机 DNS。"
        )
        return notes

    # hosted：非公网解析结果默认拒绝，但先给"代理占位符"一个自证的机会——
    # 判据不是"段"，而是"公网 DNS 认为这个域名指向哪里"。
    if settings.dns_crosscheck_enabled:
        verdict = _doh_public_verdict(host)
        if verdict is True and not public:
            notes.append(
                f"{host} 在本机解析到非公网地址 {detail}，但公网 DNS 认为该域名指向公网地址 "
                "→ 判定为代理占位/企业分流，hosted 模式放行"
            )
            return notes
        if verdict is True and public:
            raise RepoError(
                f"{host} 同时解析出公网地址（{'、'.join(f.address for f in public)}）和非公网地址"
                f"（{detail}）——这是 DNS rebinding 的典型特征，hosted 模式拒绝。"
                "若这是你自己的分流 DNS，用 PAPERLENS_REPO_ALLOW_CIDRS=<该网段> 显式信任。"
            )
        if verdict is False:
            raise RepoError(
                f"{host} 解析到了非公网地址：{detail}，且公网 DNS 也没有该域名的公网 A 记录"
                "——判定为真实内网目标，PAPERLENS_REPO_NETWORK_MODE=hosted 下拒绝。"
                "若这是你自己的内网镜像（自建 GitLab 等），用 PAPERLENS_REPO_ALLOW_CIDRS=<该网段> "
                "显式信任，或设 PAPERLENS_REPO_NETWORK_MODE=local（仅单机自用）。"
            )
        raise RepoError(
            f"{host} 解析到了非公网地址：{detail}，而公网 DoH 核验不可达"
            f"（端点：{'、'.join(settings.repo_doh_endpoints)}），hosted 模式按拒绝处理。"
            "可以：① 用 PAPERLENS_REPO_ALLOW_CIDRS=<该网段> 显式信任；"
            "② 设 PAPERLENS_REPO_DNS_CROSSCHECK=off 并在 PAPERLENS_REPO_ALLOW_CIDRS 里列全；"
            "③ 单机自用就设 PAPERLENS_REPO_NETWORK_MODE=local。"
        )
    raise RepoError(
        f"{host} 解析到了非公网地址：{detail}，PAPERLENS_REPO_NETWORK_MODE=hosted 且未启用 DoH "
        "交叉核验（PAPERLENS_REPO_DNS_CROSSCHECK=off），按拒绝处理。"
        "用 PAPERLENS_REPO_ALLOW_CIDRS=<该网段> 显式信任，或单机自用改回 local 模式。"
    )


def validate_repo_url(url: str) -> list[str]:
    """校验用户填的仓库地址。不通过就抛 RepoError，且理由要说人话。

    返回值是**提示**（notes）：比如"本机解析到代理占位地址，已放行"，调用方应该把它
    带到 run meta / 前端，让用户知道守卫做了什么决定、为什么。
    """
    notes: list[str] = []
    url = (url or "").strip()
    if not url:
        raise RepoError("仓库地址为空")

    # 本地路径只在测试开关打开时允许（公网部署绝不能开）
    if not url.startswith(("http://", "https://")):
        if not settings.allow_local_repo_paths:
            raise RepoError(
                "本地路径默认不可用（仅测试需要时打开 PAPERLENS_ALLOW_LOCAL_REPO_PATHS）；"
                "请填 https 的托管站地址"
            )
        candidate = Path(url.replace("file://", "", 1))
        if candidate.exists():
            # 本地路径 = 测试/演示模式，这个提示会走到前端时间线上：
            # 既让"守卫为什么放行"可见，也提醒不要在生产部署里打开这个开关。
            notes.append(
                "本地仓库路径（PAPERLENS_ALLOW_LOCAL_REPO_PATHS=true）：仅测试/演示用，公网部署绝不能开。"
            )
            return notes
        raise RepoError(f"本地仓库路径不存在：{url}")

    parsed = urlparse(url)
    if parsed.scheme == "http":
        raise RepoError(
            "只允许 https 的仓库地址（http 会被中间人替换代码）。"
            "内网 GitLab 若只有 http，请先在它前面放一层 https 反代"
        )
    if parsed.scheme != "https":
        raise RepoError(f"不支持的协议：{parsed.scheme or '（空）'}（只允许 https）")
    if parsed.username or parsed.password:
        raise RepoError("仓库地址里不要带用户名/密码")
    host = (parsed.hostname or "").lower()
    if not host:
        raise RepoError("仓库地址里没有主机名")

    allowed = _allowed_host_ports()
    if host not in allowed:
        raise RepoError(
            f"只允许这些代码托管站：{', '.join(str(item) for item in settings.repo_allowed_hosts)}"
            f"（当前是 {host}）。要支持别的托管站（Gitee、GitHub Enterprise、自建 GitLab…）"
            f"就设 PAPERLENS_REPO_ALLOWED_HOSTS={','.join(str(item) for item in settings.repo_allowed_hosts)},{host}"
            "（非 443 端口写成 host:port），然后重启后端"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise RepoError(f"端口不合法：{parsed.netloc}") from exc
    port = 443 if port is None else port
    if port not in allowed[host]:
        raise RepoError(
            f"只允许 {sorted(allowed[host])} 端口（当前是 {port}）。"
            f"要放行这个端口就显式声明：PAPERLENS_REPO_ALLOWED_HOSTS=...,{host}:{port}"
            f"（要同时允许默认的 443，就再加一条不带端口的 {host}）"
        )

    # 字面 IP 且已在白名单里 = 部署者的显式信任（他能写出来，就说明知道自己在连哪）
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        notes.append(f"{host} 是字面 IP 且已在白名单里显式声明，跳过地址判定（部署者的显式信任）")
        return notes

    proxy_var = _proxy_env_var()
    if proxy_var:
        notes.append(
            f"检测到 {proxy_var}：域名由代理解析，跳过后端本机 DNS 判定"
            f"（本机解析结果在代理网络里不代表目的地）"
        )
        return notes

    return notes + _judge_resolved_addresses(host, resolve_host_addresses(host))


# ---------------------------------------------------------------------------
# 克隆
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RepoInfo:
    url: str
    commit_sha: str
    files_total: int
    bytes: int
    root: Path
    seconds: float
    truncated_scan: bool = False
    skip_dirs: list[str] = field(default_factory=list)
    from_cache: bool = False
    # 地址判定过程中给用户看的提示（如"本机解析到代理占位地址，已放行"）。
    # 守卫做了决定就必须留痕，否则用户永远不知道为什么"这次没拦"。
    resolution_notes: list[str] = field(default_factory=list)
    # 体积口径（2026-09-14 拆开）：bytes/files_total 是**工作区**（Agent 看得见的内容），
    # git_bytes 是我们为了核验与按需取 blob 持有的对象库；disk_bytes = 两者之和，
    # 体积上限管的是 disk_bytes（我们到底占了用户多少磁盘）。
    git_bytes: int = 0
    disk_bytes: int = 0
    files_tracked: int = 0
    skipped_files: int = 0
    sparse: bool = False

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "commit_sha": self.commit_sha,
            "files_total": self.files_total,
            "mb": round(self.bytes / 1024 / 1024, 2),
            "seconds": round(self.seconds, 1),
            "from_cache": self.from_cache,
            "notes": list(self.resolution_notes),
            "files_tracked": self.files_tracked,
            "skipped_files": self.skipped_files,
            "git_mb": round(self.git_bytes / 1024 / 1024, 2),
            "disk_mb": round(self.disk_bytes / 1024 / 1024, 2),
            "sparse": self.sparse,
        }


def _git_env() -> dict[str, str]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",     # 绝不卡在交互式账号密码提示
        "GIT_ASKPASS": "/bin/true",
        "GIT_LFS_SKIP_SMUDGE": "1",     # 不下 LFS 大文件
        "GIT_ADVICE": "0",
    }
    if settings.isolate_git_config:
        # hosted：隔离系统/全局 git 配置——用户的 insteadOf 重写规则理论上能把白名单
        # 域名重定向到别处，绕过 URL 校验。
        #
        # local（默认）**不隔离**：企业用户就是靠 `git config --global` 配
        # http.proxy / http.sslCAInfo（MITM 代理的 CA）/ insteadOf（内网镜像）才能克隆；
        # 隔离掉他们永远跑不通，而报错完全指不到真因（2026-09-14 实测：
        # GIT_CONFIG_GLOBAL=/dev/null 下 `git config --global --get http.proxy` 读不到）。
        # 单机自用场景下，"用户的 git 配置被信任"和"用户能自己 git clone"是同一件事。
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    return env


def run_git(args: list[str], cwd: Path, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """唯一允许调用 git 的入口：不走 shell、禁 hooks、有超时。

    超时要翻译成 RepoError：subprocess 抛的 TimeoutExpired 不是本模块的"可预期失败"类型，
    上层只认 RepoError —— 不翻译的话用户会看到裸的 Python 异常名，
    验收脚本也会因为"非预期异常"整段崩掉（2026-09-15 实测：ls-remote 卡 20s 就发生了）。
    """
    command = ["git", "-c", "core.hooksPath=/dev/null", "--no-pager", *args]
    try:
        return subprocess.run(
            command,
            cwd=str(Path(cwd).resolve()),
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoError(
            f"git 命令超时（{timeout}s）：{' '.join(str(a) for a in args[:3])}…。"
            "网络慢或代理卡住时重试即可；克隆/稀疏检出相关超时可用 "
            "PAPERLENS_REPO_CLONE_TIMEOUT_SECONDS 调大。"
        ) from exc


# ---------------------------------------------------------------------------
# 克隆（ls-remote 预检 + (URL, sha) 本地缓存）
# ---------------------------------------------------------------------------
REPO_CACHE_DIRNAME = "repo-cache"   # 在 data/ 之下：run 清理不碰它


def resolve_head(url: str, *, timeout: int = 20) -> str:
    """git ls-remote 预检：仓库在不在、默认分支 HEAD 是哪个 commit。

    一次往返（几百字节）先探明三件事：
    1. 仓库不存在/无权限 → 立刻给人话，不用等整个 clone 磁到超时；
    2. 拿到 HEAD 的 sha —— 它是缓存键的一半；
    3. 空仓库 → 明确报错。
    """
    validate_repo_url(url)
    result = run_git(["ls-remote", url, "HEAD"], cwd=Path.cwd(), timeout=timeout)
    if result.returncode != 0:
        message = (result.stderr or "").strip().splitlines()
        raise RepoError(f"仓库预检失败（ls-remote）：{message[-1] if message else '未知错误'}")
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        sha = sha.strip()
        if ref.strip() == "HEAD" and re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    raise RepoError("仓库预检失败：远端没有任何提交（空仓库）")


def _normalize_repo_url(url: str) -> str:
    normalized = url.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def _cache_entry(url: str, sha: str) -> Path:
    """缓存条目目录：data/repo-cache/<sha256(规范化URL|sha|策略)[:16]>/。

    一定返回**绝对路径**：这些路径会作为 run_git 的参数或 cwd，而 run_git 的 cwd
    是调用方给的别的目录——相对路径在这里会被解析到错误位置（同「相对路径 + cwd
    错位」那个老教训）。
    键里带策略：全量克隆与"部分克隆 + 稀疏检出"的产物不是一回事，不能互相命中
    （否则旧的全量条目会被当成源码视图复用，体积照旧）。
    """
    strategy = "sparse" if settings.repo_sparse else "full"
    key = hashlib.sha256(
        f"{_normalize_repo_url(url)}|{sha}|{strategy}".encode("utf-8")
    ).hexdigest()[:16]
    return (settings.data_dir / REPO_CACHE_DIRNAME / key).resolve()


def _cache_entry_ready(entry: Path, sha: str) -> bool:
    if not (entry / ".git").exists():
        return False
    result = run_git(["rev-parse", "HEAD"], cwd=entry, timeout=15)
    return result.returncode == 0 and result.stdout.strip() == sha


def _copy_repo_tree(src: Path, dst: Path, *, hardlink_objects: bool = True) -> bool:
    """把一个仓库目录复制成缓存副本：`.git/objects` 用硬链接，其余正常复制。

    **为什么不用 `git clone` 做缓存**（2026-09-14 实测）：部分克隆的仓库里有一批
    "被承诺但没下载"的对象，git 在做本地克隆时会在服务端禁用延迟获取，于是
    pack-objects 直接失败——`致命错误：无法从承诺者远程获取 <oid>`、returncode=128。
    缓存是"尽力而为"的（失败静默返回），所以这个 bug 表现为**缓存无声失效**：
    每次定位都重新下载。硬链接的对象库没有这个问题，同文件系统内几乎零成本。

    只硬链接 objects：`.git/index`、`.git/info/sparse-checkout` 这些会被 sparse-checkout
    就地改写的文件必须各留一份，否则缓存条目和 run 目录会互相污染状态。
    """
    import shutil

    objects_root = (src / ".git" / "objects").resolve()

    def _copy(entry_src: str, entry_dst: str) -> str:
        if hardlink_objects:
            try:
                if objects_root in Path(entry_src).resolve().parents:
                    os.link(entry_src, entry_dst)
                    return entry_dst
            except OSError:
                pass                      # 跨文件系统/链接数上限：退回真拷贝
        return shutil.copy2(entry_src, entry_dst)

    try:
        _rmtree(dst)
        shutil.copytree(src, dst, copy_function=_copy, symlinks=True)
        return True
    except Exception:  # noqa: BLE001 —— 缓存是尽力而为，失败只影响下次命中
        _rmtree(dst)
        return False


def _populate_cache(url: str, sha: str, source: Path) -> None:
    """把刚克隆好的仓库回填进缓存（硬链接复制，几乎零成本）。

    尽力而为：任何失败只影响下次缓存命中，不影响本次定位。
    """
    entry = _cache_entry(url, sha)
    entry.parent.mkdir(parents=True, exist_ok=True)
    if _cache_entry_ready(entry, sha):
        return
    # 先复制到临时目录再原子改名：两个 run 并发回填同一键时不会写坏缓存
    staging = entry.parent / f".staging-{entry.name}-{os.getpid()}"
    if not _copy_repo_tree(source, staging):
        return
    _rmtree(entry)
    try:
        staging.rename(entry)
    except OSError:
        _rmtree(staging)  # 另一个进程已经放好了同一个键
        return
    _evict_cache()


def _evict_cache(keep: int = 32) -> None:
    """缓存按修改时间只留最近 keep 个条目，防止无界增长。"""

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    cache_root = (settings.data_dir / REPO_CACHE_DIRNAME).resolve()
    if not cache_root.exists():
        return
    entries = sorted(
        (p for p in cache_root.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=_mtime,
    )
    for stale in entries[:-keep] if len(entries) > keep else []:
        _rmtree(stale)


def _clone_timeout_message() -> str:
    return (
        f"克隆超时（超过 {settings.repo_clone_timeout_seconds}s），已清理。"
        "网络慢（如经代理访问 GitHub）可以调大：启动前设 "
        f"PAPERLENS_REPO_CLONE_TIMEOUT_SECONDS=600。另外注意 {settings.repo_max_mb}MB 体积上限仍然生效。"
    )


def _run_git_streaming(
    args: list[str],
    cwd: Path,
    *,
    timeout: int,
    on_progress: "Callable[[str], None] | None" = None,
    timeout_hint: str | None = None,
) -> tuple[int, str]:
    """带实时 stderr 进度的 git 调用（仅 clone 用，其余 git 命令走 run_git）。

    为什么不能复用 run_git：git 的进度信息写在 **stderr** 且用 `\\r` 原地刷新
    （"Receiving objects: 33%...\rReceiving objects: 66%..."），subprocess.run
    要等进程结束才能拿到，用户在 300s 超时内什么都看不到。这里用 Popen 按字节
    读 stderr，按 \\r/\\n 切分成行，经 on_progress（限频）实时吐出去。

    超时杀进程并抛 RepoError（文案与整条链路一致）。返回 (returncode, 完整 stderr)。
    """
    command = ["git", "-c", "core.hooksPath=/dev/null", "--no-pager", *args]
    proc = subprocess.Popen(
        command,
        cwd=str(Path(cwd).resolve()),
        env=_git_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,  # 无缓冲：os.read 才能读到 \r 刷新的中间帧
    )
    assert proc.stderr is not None
    fd = proc.stderr.fileno()
    sel = selectors.DefaultSelector()
    sel.register(proc.stderr, selectors.EVENT_READ)

    started = time.monotonic()
    last_emit = 0.0
    buffer = b""
    collected: list[bytes] = []

    def _feed(chunk: bytes) -> None:
        """把 stderr 字节流按 \r/\n 切成行，限频后交给 on_progress。"""
        nonlocal buffer, last_emit
        buffer += chunk
        collected.append(chunk)
        parts = re.split(rb"[\r\n]+", buffer)
        buffer = parts[-1]  # 最后一段可能还没被 \r/\n 结束
        now = time.monotonic()
        for raw in parts[:-1]:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or on_progress is None:
                continue
            is_final = line.endswith(("done.", "完成。"))
            if last_emit == 0.0 or is_final or now - last_emit >= 0.4:
                last_emit = now
                try:
                    on_progress(line)
                except Exception:  # noqa: BLE001 —— 进度回调绝不能弄死克隆
                    pass

    try:
        while proc.poll() is None:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                proc.kill()
                proc.wait(timeout=5)
                raise RepoError(timeout_hint or _clone_timeout_message())
            for key, _ in sel.select(timeout=min(0.25, remaining)):
                chunk = os.read(fd, 65536)
                if chunk:
                    _feed(chunk)
        # 进程已退出，把管道里剩下的内容读完（EOF 自然结束）
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            _feed(chunk)
        proc.wait(timeout=5)
    finally:
        sel.close()
        if proc.stderr:
            proc.stderr.close()

    return proc.returncode, b"".join(collected).decode("utf-8", errors="replace")


def clone_repo(url: str, dest: Path, *, overwrite: bool = False, on_progress=None) -> RepoInfo:
    resolution_notes = validate_repo_url(url)
    started = time.monotonic()
    # 一定要绝对路径：subprocess 的 cwd 与 git 自己解析相对路径的基准不同，
    # 混用会出现"克隆到了 data/data/<id>/repo，然后去 data/<id>/repo 找 HEAD"这种鬼故事
    dest = Path(dest).expanduser().resolve()
    if dest.exists():
        if not overwrite:
            raise RepoError(f"目标目录已存在：{dest}")
        # 定位是可重入的：同一个 run 第二次点「开始定位」（换勾选、补定位、失败重试）
        # 必须能跑通，所以调用方显式要求覆盖时，先清掉上一次的克隆再重来。
        # repo/ 是纯派生数据（每次都从源头重新克隆），删掉没有信息损失。
        _rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 预检：一次 ls-remote 拿到 HEAD 的 sha（失败立刻给人话，也决定缓存键）
    head_sha = resolve_head(url)
    sparse_wanted = settings.repo_sparse and _git_supports_sparse_checkout()
    if settings.repo_sparse and not sparse_wanted:
        resolution_notes.append("git 版本低于 2.25，不支持稀疏检出，已退回全量克隆")

    from_cache = False
    entry = _cache_entry(url, head_sha)
    if _cache_entry_ready(entry, head_sha):
        # 缓存命中：硬链接复制（对象库共享 inode，秒级）。
        # 依据（2026-09-12 实测）：从浅克隆做本地副本是允许的，产物同样是钉在同一个
        # commit 的单提交仓库；缓存条目日后被清理也不影响已克隆的 run（硬链接语义，
        # 删一方另一方还在）。2026-09-14 改成复制而不是 `git clone`：部分克隆里
        # "被承诺但没下载"的对象会让本地克隆的 pack-objects 直接失败（见 _copy_repo_tree）。
        if _copy_repo_tree(entry, dest):
            from_cache = True
            if on_progress is not None:
                try:
                    # 缓存命中不再有 git 的输出，但时间线上必须看得见"这一步没走网络"
                    on_progress("命中本地克隆缓存：从缓存硬链接复制（未走网络）")
                except Exception:  # noqa: BLE001 —— 进度回调绝不能弄死克隆
                    pass
        else:
            _rmtree(dest)  # 缓存条目坏了：清掉它，回退走网络
            _rmtree(entry)

    if not from_cache:
        # --progress 强制 git 在 stderr 上报实时进度（非 TTY 默认不给）；
        # 注意不能同时用 --quiet——quiet 会把进度一起吞掉。
        try:
            returncode, stderr_text = _run_git_streaming(
                _clone_command(url, dest, sparse=sparse_wanted, partial=sparse_wanted),
                cwd=dest.parent,
                timeout=settings.repo_clone_timeout_seconds,
                on_progress=on_progress,
            )
        except FileNotFoundError as exc:
            raise RepoError("这台机器上没有 git，无法克隆仓库") from exc
        if returncode != 0:
            message = stderr_text.strip().splitlines()
            raise RepoError(f"克隆失败：{message[-1] if message else '未知错误'}")

    # origin 必须指着真正的上游：部分克隆的缺失 blob 是"按需去 origin 取"的，而缓存命中
    # 路径的 origin 会是缓存目录（本地路径）——留成那样等于取不到对象。
    run_git(["remote", "set-url", "origin", url], cwd=dest, timeout=15)

    # 稀疏规则决定工作区里有哪些文件（源码全取，媒体/权重/依赖目录不取）。
    # 缓存命中路径也跑一遍：本地副本不一定继承 sparse 状态，这里统一收敛。
    if sparse_wanted:
        # 先刷新索引的 stat 缓存：git 只在"索引与工作区一致"时才真正删掉被排除的路径，
        # 否则它会**保留**它们、只打一条警告（"以下路径不是最新，因而保留"）。
        # 复制出来的副本（文件 inode/ctime 都变了）必然被判成"不是最新"——实测缓存副本上
        # 跑 sparse-checkout set 会静默失效：node_modules 还在、skip 计数为 0、体积也没降。
        run_git(["update-index", "--refresh"], cwd=dest, timeout=settings.repo_clone_timeout_seconds)
        code, sparse_error = _run_git_streaming(
            ["sparse-checkout", "set", "--no-cone", *sparse_exclude_patterns(dest)],
            cwd=dest,
            timeout=settings.repo_clone_timeout_seconds,
            on_progress=on_progress,
            timeout_hint=(
                "稀疏检出超时（按需取源码文件太慢），已中止。可以调大 "
                "PAPERLENS_REPO_CLONE_TIMEOUT_SECONDS，或设 PAPERLENS_REPO_SPARSE=false 退回全量克隆。"
            ),
        )
        if code != 0:
            # 不要在这里"退回完整检出"：那会把一次网络抖动变成把整个仓库拖下来
            # （实测：稀疏步骤失败 → 全量检出 237MB → 撞体积上限，报出来的却是一句
            # "仓库体积超过上限"，完全指不到真因）。如实报错，让用户重试或显式关掉。
            tail = (sparse_error or "").strip().splitlines()
            _rmtree(dest)
            raise RepoError(
                f"稀疏检出失败（只取源码视图这一步）：{tail[-1] if tail else '未知错误'}。"
                "网络抖动时重试即可；也可设 PAPERLENS_REPO_SPARSE=false 退回全量克隆。"
            )

    # ---- 先量体积再决定留不留：被拒的仓库不该占磁盘、也不该进缓存 ----
    work_bytes, files_total, git_bytes, truncated = _measure(dest)
    files_tracked, skipped_files, skipped_example = _sparse_status(dest)
    if not files_tracked:                       # git 太老 / 异常时的兜底口径
        files_tracked = _tracked_file_count(dest)
        skipped_files = max(0, files_tracked - files_total)
    disk_bytes = work_bytes + git_bytes

    limit = settings.repo_max_mb * 1024 * 1024
    if disk_bytes > limit:
        _rmtree(dest)
        raise RepoError(
            f"仓库体积 {disk_bytes / 1024 / 1024:.1f}MB"
            f"（工作区 {work_bytes / 1024 / 1024:.1f}MB + git 对象库 {git_bytes / 1024 / 1024:.1f}MB）"
            f" 超过 {settings.repo_max_mb}MB 上限，已清理。"
            + (
                "对象库远大于工作区，通常说明该托管站不支持部分克隆（--filter 被忽略），"
                "整库对象都被下载了；"
                if skipped_files and git_bytes > work_bytes * 2
                else ""
            )
            + "可以调大 PAPERLENS_REPO_MAX_MB，或检查 PAPERLENS_REPO_SPARSE 是否被关掉了。"
        )

    # 过关了才回填缓存（原顺序：先回填再量体积 → 被拒的仓库照样占着缓存 238MB，实测过）
    if not from_cache:
        _populate_cache(url, head_sha, dest)

    revision = run_git(["rev-parse", "HEAD"], cwd=dest, timeout=15)
    if revision.returncode != 0:
        raise RepoError("克隆后拿不到 commit 号")
    commit_sha = revision.stdout.strip()
    if commit_sha != head_sha:
        raise RepoError(
            f"克隆到的 HEAD（{commit_sha[:12]}…）与预检结果（{head_sha[:12]}…）不一致，已中止"
        )

    if sparse_wanted:
        # 只报**能确证的事实**：工作区有多少文件/多大、对象库多大、git 说跳过了哪些。
        # 不猜"服务器到底有没有真的只传 blob"（三种判据都不可靠，见 _partial_clone_active）。
        note = (
            f"已启用源码视图克隆：工作区 {files_total} 个文件 / {work_bytes / 1024 / 1024:.1f}MB，"
            f"git 对象库 {git_bytes / 1024 / 1024:.1f}MB"
        )
        if skipped_files:
            note += (
                f"；按体积规则跳过 {skipped_files} 个文件未检出"
                f"（{skipped_example} 等，媒体/权重/依赖目录，工具本来也不读）"
            )
        heavy = unreadable_heavy_files(dest)
        if heavy:
            # 后缀表之外的冷门二进制格式没法预判，但可以点名 + 给出该加的那条规则
            names = "、".join(f"{name}（{size / 1024 / 1024:.1f}MB）" for size, name in heavy)
            suffixes = sorted({Path(name).suffix or name for _, name in heavy})
            note += (
                f"；另有 {len(heavy)} 个较大的二进制文件被下载了但工具不会读：{names}——"
                f"要连它们也不下载，设 PAPERLENS_REPO_SPARSE_EXCLUDE={','.join('!' + s for s in suffixes)}"
            )
        resolution_notes.append(note)

    return RepoInfo(
        url=url,
        commit_sha=commit_sha,
        files_total=files_total,
        bytes=work_bytes,
        root=dest,
        seconds=time.monotonic() - started,
        truncated_scan=truncated,
        skip_dirs=sorted(SKIP_DIRS),
        from_cache=from_cache,
        resolution_notes=resolution_notes,
        git_bytes=git_bytes,
        disk_bytes=disk_bytes,
        files_tracked=files_tracked,
        skipped_files=skipped_files,
        sparse=sparse_wanted,
    )


# ---------------------------------------------------------------------------
# 克隆策略：只取"源码视图"（2026-09-14）
# ---------------------------------------------------------------------------
# 原来用 `--depth 1` 全量检出：把仓库当文件转储整包拉下来，再把体积上限当"代码规模上限"用。
# 实测 zju3dv/INTACT-JEPA：为了读 121 个文本文件（1.8MB）下载了 237MB——
# 工作区 140MB 里 6 个 docs/assets/*.mp4 演示视频占 83MB，另有 .git 97MB（同一份内容的
# 第二份拷贝）。于是"仓库作者往 docs/ 里放了什么"决定了"这个仓库能不能分析"。
#
# 现在的做法：`--filter=blob:none`（只取提交与目录树）+ `--sparse`（只检出源码类文件），
# 同一个仓库实测 2.5MB / 2 秒，源码一个不少、0 个视频。排除规则直接复用
# SKIP_DIRS / BINARY_SUFFIXES，保证"不下载什么"和"工具不读什么"是同一套标准。
#
# 被排除的文件仍可按需取：部分克隆会给 .git 留下 promisor 配置，真需要某个 blob 时
# `git show` 会去 origin 取那一个对象（核验强度不变，也就不必为了核验把全库拖下来）。
SPARSE_INCLUDE_ALL = "/*"


def sparse_exclude_patterns(dest: Path | None = None) -> list[str]:
    """稀疏检出的排除规则（no-cone 模式，gitignore 风格负向模式）。

    三层来源，都是为了"不下载工具根本不会读的东西"：
    1. SKIP_DIRS（依赖/构建/缓存目录）与 BINARY_SUFFIXES（二进制/权重/媒体后缀）——
       与读取层同一套标准，"不下载什么"和"不读什么"不会分叉；
    2. 仓库自己在 `.gitattributes` 里声明的 binary / filter=lfs 模式（作者最清楚哪些是资产）；
    3. 用户用 PAPERLENS_REPO_SPARSE_EXCLUDE 追加的规则。
    """
    patterns = [SPARSE_INCLUDE_ALL]
    patterns += [f"!{name}" for name in sorted(SKIP_DIRS) if name != ".git"]
    patterns += [f"!*{suffix}" for suffix in sorted(BINARY_SUFFIXES)]
    if dest is not None:
        patterns += _gitattributes_excludes(dest)
    patterns += [str(item).strip() for item in settings.repo_sparse_exclude if str(item).strip()]
    # 去重：后缀表与 .gitattributes 经常声明同一条（实测 `!*.png` 会重复），
    # 规则表是给人看的，重复只会让人怀疑哪条生效。
    return list(dict.fromkeys(patterns))


def _gitattributes_excludes(dest: Path, *, limit: int = 200) -> list[str]:
    """把仓库 `.gitattributes` 里声明为 `binary` / `filter=lfs` 的模式也排除。

    为什么读它：我们的后缀表永远漏冷门格式，而仓库作者通常已经在 `.gitattributes`
    里标了"这是二进制资产"。
    为什么要过滤：这是**仓库内容（不可信输入）驱动的行为**——一条 `*` 就能把工作区清空。
    所以只接受"看起来指向具体路径或后缀"的模式（含 `.` 或 `/`）、拒绝以 `!` 开头的模式，
    并且限量。
    """
    path = dest / ".gitattributes"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    patterns: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        candidate, attrs = parts[0], " ".join(parts[1:]).lower()
        if "binary" not in attrs and "filter=lfs" not in attrs:
            continue
        if candidate.startswith("!") or candidate in {"*", "/*", "**"}:
            continue                                  # 能清空整个工作区，一律不认
        if "." not in candidate and "/" not in candidate:
            continue                                  # 只认"像路径/像后缀"的
        if candidate in patterns or len(patterns) >= limit:
            continue
        patterns.append(candidate)
    return [f"!{item}" for item in patterns]


def unreadable_heavy_files(root: Path, *, top: int = 3, min_bytes: int = 1024 * 1024) -> list[tuple[int, str]]:
    """工作区里"体积大、但工具本来也不会读"的文件（按体积倒序）。

    用途是给用户一条可执行的出路：后缀表之外的冷门二进制格式（实测
    `docs/assets/**/*.i16` 6 个文件 26MB）我们没法预判，但可以在提示里点名，
    并给出该加哪条 PAPERLENS_REPO_SPARSE_EXCLUDE。
    """
    found: list[tuple[int, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < min_bytes or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        if is_probably_text(path):
            continue                                   # 文本，工具要读，别劝用户排除
        found.append((size, str(path.relative_to(root))))
    found.sort(reverse=True)
    return found[:top]


def _clone_command(url: str, dest: Path, *, sparse: bool, partial: bool) -> list[str]:
    """构造 clone 参数（纯函数，可离线验收）。"""
    args = [
        "clone",
        "--progress",                    # --progress 强制在 stderr 上报进度（非 TTY 默认不给）
        "--depth", "1",
        "--single-branch",
        "--no-tags",
        "--no-recurse-submodules",       # submodule 可以在 checkout 时执行任意代码
    ]
    if sparse:
        args.append("--sparse")
    if partial:
        # 只取提交与目录树；blob 按需取。服务器不支持时 git 会警告并忽略（我们事后核对）。
        args.append("--filter=blob:none")
    args += [url, str(dest)]
    return args


def _partial_clone_active(dest: Path) -> bool:
    """**不要用这个判断"部分克隆是否真的生效"**（保留仅为记录这次踩到的坑）。

    试过三种判据，全都不可靠：
    1. `remote.origin.partialclonefilter` —— git 即使在"服务器不支持 / 本地克隆忽略 filter"
       的情况下也会照样写这个配置（实测：file:// 克隆打印"filtering not recognized by server,
       ignoring"，配置里却仍有 partialclonefilter=blob:none）；
    2. git 的告警文案 —— 输出是本地化的（中文 git 打"警告：--filter … 被忽略"），字符串匹配必踩坑；
    3. `git rev-list --objects --all --missing=print` —— 部分克隆里"被承诺但没下载"的对象
       不算 missing，恒为 0。
    所以现在不猜：直接把两个数字摊开给用户看（工作区 MB 与对象库 MB），
    对象库明显大于工作区就说明 filter 没生效——见 clone_repo 里的提示文案。
    """
    result = run_git(["config", "--get", "remote.origin.partialclonefilter"], cwd=dest, timeout=15)
    return result.returncode == 0 and "blob:none" in result.stdout


def _tracked_file_count(dest: Path) -> int:
    """HEAD 上被跟踪的文件数（**不带 `-l`**）。

    为什么强调不带 `-l`：`git ls-tree -l` 要打印文件大小，就得把 blob 取下来——
    在部分克隆上这会偷偷把整个对象库拉回来（实测 .git 284KB → 99MB）。只读树不取 blob。
    子模块（mode 160000）不算文件：我们本来就不递归子模块。
    """
    result = run_git(["ls-tree", "-r", "HEAD"], cwd=dest, timeout=30)
    if result.returncode != 0:
        return 0
    count = 0
    for line in result.stdout.splitlines():
        if line.startswith(("100", "120000")):   # 普通文件 / 符号链接
            count += 1
    return count


def _sparse_status(dest: Path) -> tuple[int, int, str | None]:
    """(被跟踪文件数, 被稀疏规则跳过的文件数, 第一个被跳过的路径)。

    用 `git ls-files -t`：git 自己会在索引里把稀疏跳过的条目标成 `S`（skip-worktree），
    已检出的标 `H`。比"自己走目录树再和 ls-tree 求差集"更准也更便宜。
    """
    result = run_git(["ls-files", "-t"], cwd=dest, timeout=30)
    if result.returncode != 0:
        return (0, 0, None)
    total = 0
    skipped = 0
    first: str | None = None
    for line in result.stdout.splitlines():
        if len(line) < 3:
            continue
        tag, path = line[0], line[2:].strip()
        total += 1
        if tag == "S":
            skipped += 1
            if first is None:
                first = path
    return (total, skipped, first)


def _git_supports_sparse_checkout() -> bool:
    """`--sparse` / `sparse-checkout` 需要 git ≥ 2.25（2020 年）。"""
    result = run_git(["--version"], cwd=Path.cwd(), timeout=15)
    if result.returncode != 0:
        return False
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2))) >= (2, 25)


def _measure(root: Path) -> tuple[int, int, int, bool]:
    """量三样东西：工作区字节数、工作区文件数、.git 字节数（+ 是否因为文件太多而截断）。

    为什么要分开：工作区是 Agent 真正看得见、读得到的内容（也是界面上"X 个文件 · Y MB"
    该显示的东西）；.git 是我们为了核验和按需取 blob 必须占的磁盘。原来把两者混在一起
    统计，于是 (1) 体积上限实际管的是"工作区 + 同一份内容的压缩拷贝"，(2) 界面上的文件数
    把 .git 里的内部文件也算了进去（夹具仓库显示 50 个，工作区其实只有 13 个）。
    """
    work_bytes = 0
    work_files = 0
    git_bytes = 0
    truncated = False
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if ".git" in path.parts:
            git_bytes += size
            continue
        work_files += 1
        work_bytes += size
        if work_files > 50_000:
            truncated = True
            break
    return work_bytes, work_files, git_bytes, truncated


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 读取（Agent 的工具靠它）
# ---------------------------------------------------------------------------
def is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    try:
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return False
    return b"\0" not in chunk


def normalize_lines(text: str) -> str:
    """核验用归一化：统一换行、去行尾空白。做哈希和比对都用它。"""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip("\n")


def text_lines(content: str) -> list[str]:
    """统一的「行」口径：Agent 看到的行号（read_file）、核验的行号（verify.py）、
    前端看的行号（/api/runs/{id}/file）必须数出同一份行数。

    之前三处各写各的：read_file 用 splitlines()，verify 用 normalize_lines().split("\\n")
    （它会把开头/结尾的空行整个剥掉），/file 用 split("\\n") 再弹掉一个尾空行——
    文件开头或结尾有连续空行时，Agent 按 read_file 报告的行号提交的引用
    会被核验误判成「行区间非法」。都统一到 splitlines() 上。
    """
    return content.replace("\r\n", "\n").replace("\r", "\n").splitlines()


class RepoSource:
    """按需读取一个已克隆的仓库。**只读，不执行任何东西。**"""

    def __init__(self, root: Path, commit_sha: str) -> None:
        self.root = Path(root).resolve()
        self.commit_sha = commit_sha

    # -- 路径安全 -----------------------------------------------------------
    def resolve(self, relative: str) -> Path:
        if not relative or relative.strip() in {"", "."}:
            raise RepoError("路径不能为空")
        if relative.startswith("-"):
            raise RepoError("路径不能以 - 开头")
        if "\0" in relative:
            raise RepoError("路径里不能有 NUL 字节")
        target = (self.root / relative).resolve()
        if target != self.root and self.root not in target.parents:
            raise RepoError(f"路径越界：{relative} 不在仓库目录内")
        return target

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # -- 遍历 ---------------------------------------------------------------
    def iter_files(self, subdir: str = "", *, limit: int = MAX_SEARCH_FILES) -> list[Path]:
        base = self.resolve(subdir) if subdir else self.root
        if base.is_file():
            return [base]
        out: list[Path] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(self.root).parts[:-1]):
                continue
            out.append(path)
            if len(out) >= limit:
                break
        return out

    def tree(self, subdir: str = "", depth: int = 2, max_rows: int = 400) -> dict:
        base = self.resolve(subdir) if subdir else self.root
        if not base.exists():
            raise RepoError(f"目录不存在：{subdir or '.'}")
        rows: list[dict] = []
        truncated = False
        root_depth = len(base.relative_to(self.root).parts)
        for path in sorted(base.rglob("*")):
            relative_parts = path.relative_to(self.root).parts
            if any(part in SKIP_DIRS for part in relative_parts):
                continue
            if len(relative_parts) - root_depth > depth:
                continue
            rows.append(
                {
                    "path": path.relative_to(self.root).as_posix(),
                    "type": "dir" if path.is_dir() else "file",
                    "size": path.stat().st_size if path.is_file() else None,
                }
            )
            if len(rows) >= max_rows:
                truncated = True
                break
        return {"subdir": subdir or ".", "rows": rows, "truncated": truncated, "skipped_dirs": sorted(SKIP_DIRS)}

    # -- 读文件 -------------------------------------------------------------
    def line_count(self, relative: str) -> int:
        path = self.resolve(relative)
        if not path.is_file():
            raise RepoError(f"不是文件：{relative}")
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())

    def read_file(self, relative: str, start: int | None = None, end: int | None = None) -> dict:
        path = self.resolve(relative)
        if not path.is_file():
            raise RepoError(f"文件不存在：{relative}")
        size = path.stat().st_size
        if size > MAX_TEXT_FILE_BYTES:
            raise RepoError(f"文件 {relative} 有 {size / 1024 / 1024:.1f}MB，超过单文件读取上限")
        if not is_probably_text(path):
            raise RepoError(f"{relative} 看起来是二进制文件，读不了")

        raw = text_lines(path.read_text(encoding="utf-8", errors="replace"))
        total = len(raw)
        first = max(1, start or 1)
        last = min(end or total, total)
        if first > last:
            raise RepoError(f"行区间非法：start={first} 大于 end={last}（文件共 {total} 行）")
        numbered = "\n".join(f"{index:>5}  {raw[index - 1]}" for index in range(first, last + 1))
        return {
            "path": relative,
            "line_start": first,
            "line_end": last,
            "line_count_total": total,
            "numbered": numbered,
        }

    # -- 搜索 ---------------------------------------------------------------
    def search(
        self,
        pattern: str,
        *,
        glob: str = "**/*",
        max_hits: int = 40,
        ignore_case: bool = True,
    ) -> dict:
        """朴素的正则搜索（**不做语义检索**）。

        刻意用纯 Python 实现而不是依赖 ripgrep：少一个外部依赖、结果顺序确定、便于测试。
        代价是大仓库会慢 —— 所以有文件数与时间双重预算，并且**明确告诉 Agent 自己被截断了**，
        让它知道"没找到"不等于"不存在"。
        """
        if not pattern or not pattern.strip():
            raise RepoError("搜索模式不能为空")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise RepoError(f"正则不合法：{exc}") from exc

        started = time.monotonic()
        hits: list[dict] = []
        scanned = 0
        truncated_files = False
        timed_out = False

        for path in self.iter_files(limit=MAX_SEARCH_FILES):
            relative = self.rel(path)
            if glob not in {"**/*", "*"} and not path.match(glob):
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            scanned += 1
            if scanned > MAX_SEARCH_FILES:
                truncated_files = True
                break
            if time.monotonic() - started > MAX_SEARCH_SECONDS:
                timed_out = True
                break
            if path.stat().st_size > MAX_TEXT_FILE_BYTES or not is_probably_text(path):
                continue
            try:
                for number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
                ):
                    if regex.search(line):
                        hits.append({"path": relative, "line": number, "text": line.strip()[:240]})
                        if len(hits) >= max_hits:
                            break
            except OSError:
                continue
            if len(hits) >= max_hits:
                break

        return {
            "pattern": pattern,
            "hits": hits,
            "files_scanned": scanned,
            "complete": not (truncated_files or timed_out or len(hits) >= max_hits),
            "note": (
                "搜索被截断（达到命中数/文件数/时间上限之一），"
                "所以'没找到'不等于'不存在'，换个更精确的模式再来一次。"
                if (truncated_files or timed_out or len(hits) >= max_hits)
                else "本次搜索扫完了整个仓库（跳过依赖与二进制文件）。"
            ),
        }

    # -- 核验用：直接从 git 对象里取内容（真正的"重放"，不依赖工作区是否被改动）--
    def content_at_commit(self, relative: str, *, timeout: int = 20) -> str:
        if relative.startswith("-") or "\0" in relative:
            raise RepoError("非法路径")
        result = run_git(["show", f"{self.commit_sha}:{relative}"], cwd=self.root, timeout=timeout)
        if result.returncode != 0:
            raise RepoError(f"在 commit {self.commit_sha[:8]} 上找不到文件：{relative}")
        return result.stdout

    def commit_exists(self) -> bool:
        return run_git(["cat-file", "-e", f"{self.commit_sha}^{{commit}}"], cwd=self.root, timeout=15).returncode == 0
