"""M2 离线验收：阶段 B（仓库定位 + 引用核验）。

不需要任何真实 API key。分四段：

A. 仓库层与工具护栏（不经过 LLM）
   —— URL 白名单/SSRF、克隆硬化、目录跳过、读取越界与二进制拒绝、搜索截断标记、
      verify.check_evidence 的五种失败形态、record_finding 的打回与标记逻辑
B. 端到端：上传 → 侦察 → 定位 → 产物 → 引用核验率 100%（走真实 HTTP + SSE）
C. 端到端：编造的代码引用被打回 → 修正后通过
D. 失败路径：非法仓库地址 / 没有清单就定位 / 不存在的仓库

运行：
    cd backend && .venv/bin/python -m scripts.m2_check
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import ipaddress
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from .harness import (
    APP,
    APP_PORT,
    MOCK_PORT,
    Checker,
    collect_sse,
    current_max_event_id,
    events_of,
    provider,
    start_service,
    stop_services,
    wait_http,
    ROOT,
)

from tests.paper_fixture import ensure_fixtures
from tests.repo_fixture import build_mini_repo, build_repo, layer_lines, quote_from_lines


async def expect_error(check: Checker, thunk: Any, message: str, contains: str = "") -> None:
    try:
        result = thunk()
        if inspect.isawaitable(result):
            result = await result
        check(False, f"{message}（没有抛错，返回了 {str(result)[:80]}）")
    except Exception as exc:  # noqa: BLE001
        ok = contains in str(exc) if contains else True
        check(ok, f"{message} → {type(exc).__name__}: {str(exc)[:110]}")


# ---------------------------------------------------------------------------
# A. 仓库层与工具护栏
# ---------------------------------------------------------------------------
async def section_a(check: Checker) -> dict[str, Any]:
    from app import repo_source
    from app.agent.tools.base import ToolContext
    from app.agent.tools.findings import RECORD_FINDING
    from app.agent.tools.repo_tools import REPO_TOOLS
    from app.config import settings
    from app.events import RunBus
    from app.repo_source import (
        AddressFinding,
        RepoError,
        RepoSource,
        _cache_entry,
        _classify_address,
        _copy_repo_tree,
        _doh_verdict_from_payload,
        _git_env,
        _sparse_status,
        clone_repo,
        resolve_head,
        sparse_exclude_patterns,
        text_lines,
        unreadable_heavy_files,
        validate_repo_url,
    )
    from app.verify import check_evidence, snippet_sha256, verify_artifact

    check.section("A. 仓库层与工具护栏（不经过 LLM）")
    ensure_fixtures(ROOT)
    source_repo = build_repo(ROOT)
    work = ROOT / "data" / f"unit-m2-{int(time.time() * 1000)}"
    work.mkdir(parents=True, exist_ok=True)

    # ---- URL 校验（临时改配置测护栏，测完恢复）----
    local_ok = settings.allow_local_repo_paths
    hosts_ok = settings.repo_allowed_hosts
    mode_ok = settings.repo_network_mode
    cidrs_ok = settings.repo_allow_cidrs
    legacy_ok = settings.repo_allow_private_ips
    isolate_ok = settings.repo_isolate_git_config
    crosscheck_ok = settings.repo_dns_crosscheck
    real_resolve = repo_source.resolve_host_addresses
    real_doh = repo_source._doh_public_verdict

    def pin_resolution(*rows: tuple[str, str, str]) -> None:
        """把 DNS 答案钉成构造好的分类结果——地址判定必须能离线验收。"""
        pinned = [AddressFinding(address=a, kind=k, category=c) for a, k, c in rows]
        repo_source.resolve_host_addresses = lambda host: list(pinned)

    try:
        settings.allow_local_repo_paths = False
        await expect_error(
            check,
            lambda: validate_repo_url(str(source_repo)),
            "默认配置下本地路径被拒绝",
            "本地路径",
        )
        await expect_error(check, lambda: validate_repo_url("http://github.com/x/y"), "http 被拒绝", "只允许 https")
        await expect_error(
            check,
            lambda: validate_repo_url("https://bitbucket.org/x/y"),
            "非白名单域名被拒绝",
            "只允许这些代码托管站",
        )
        await expect_error(
            check,
            lambda: validate_repo_url("https://user:pass@github.com/x/y"),
            "地址里带凭据被拒绝",
            "不要带用户名",
        )
        await expect_error(
            check,
            lambda: validate_repo_url("https://github.com:8080/x/y"),
            "非 443 端口被拒绝",
            "只允许",
        )

        # SSRF：把 localhost 临时放进白名单，就应该被"解析到内网地址"拦住
        settings.repo_allowed_hosts = ["localhost"]
        await expect_error(
            check,
            lambda: validate_repo_url("https://localhost/x/y"),
            "白名单域名解析到内网地址被拒绝",
            "内网",
        )
        # 真实解析（只走系统 resolver + hosts 文件，不依赖外网）
        check(
            bool(real_resolve("localhost"))
            and all(finding.category == "hard" for finding in real_resolve("localhost")),
            "真实解析 localhost 落到硬拦类（分类器与 getaddrinfo 接线正确）",
        )

        # ---- 2026-09-14 重做：判定跟随部署模式 + DoH 交叉核验 + 显式信任 ----
        settings.repo_allowed_hosts = hosts_ok
        settings.repo_network_mode = "local"
        settings.repo_allow_cidrs = []
        settings.repo_allow_private_ips = False
        settings.repo_dns_crosscheck = "auto"

        # 地址分类表 = 判定依据。每一项都对应一次 2026-09-14 实测（"网段彩票"现场）。
        classification = {
            "198.18.0.47": ("private", "non_public"),          # Clash/Mihomo/sing-box 默认 fake-ip
            "28.0.0.1": ("public", "public"),                  # 某些代理的 fake-ip 段（旧守卫侥幸放过）
            "240.0.0.1": ("private", "non_public"),            # 部分 fake-ip 用 240/4
            "100.64.0.1": ("non_public", "non_public"),        # CGNAT
            "64:ff9b::a00:1": ("reserved", "non_public"),      # NAT64/DNS64（IPv6-only 网络）
            "fdfe:dcba:9876::1": ("private", "non_public"),    # mihomo v6 fake-ip
            "127.0.0.1": ("loopback", "hard"),
            "169.254.169.254": ("link_local", "hard"),
            "140.82.113.3": ("public", "public"),
        }
        mismatch = [
            f"{address}:{_classify_address(ipaddress.ip_address(address))}≠{want}"
            for address, want in classification.items()
            if _classify_address(ipaddress.ip_address(address)) != want
        ]
        check(
            not mismatch,
            f"地址分类表 {len(classification)} 类（fake-ip/CGNAT/NAT64/回环/元数据/公网）{mismatch or '全部符合'}",
        )

        # (1) 用户现场的回归：本机解析到代理 fake-ip 占位地址，local 模式只提示不阻断
        pin_resolution(("198.18.0.47", "private", "non_public"))
        notes = validate_repo_url("https://github.com/x/y")
        check(
            any("198.18.0.47" in note for note in notes),
            "local 模式（默认）下 fake-ip 占位地址只提示不阻断——正是用户报 422 的那个场景",
        )
        pin_resolution(("10.1.2.3", "private", "non_public"))
        notes = validate_repo_url("https://github.com/x/y")
        check(any("10.1.2.3" in note for note in notes), "local 模式下企业分流 DNS 同样只提示不阻断")
        pin_resolution(("64:ff9b::a00:1", "reserved", "non_public"))
        notes = validate_repo_url("https://github.com/x/y")
        check(any("64:ff9b" in note for note in notes), "IPv6-only / NAT64 环境不再被 is_reserved 误杀")

        pin_resolution(("127.0.0.1", "loopback", "hard"))
        await expect_error(
            check, lambda: validate_repo_url("https://github.com/x/y"), "local 模式仍然拒绝回环地址", "回环"
        )
        pin_resolution(("169.254.169.254", "link_local", "hard"))
        await expect_error(
            check,
            lambda: validate_repo_url("https://github.com/x/y"),
            "local 模式仍然拒绝云元数据地址",
            "链路本地",
        )

        # (2) hosted：非公网解析结果默认拒绝，但"公网 DNS 认为是公网域名"就认作代理占位
        settings.repo_network_mode = "hosted"
        pin_resolution(("10.1.2.3", "private", "non_public"))
        repo_source._doh_public_verdict = lambda host: True
        notes = validate_repo_url("https://github.com/x/y")
        check(any("占位" in note for note in notes), "hosted + 公网 DoH 认为是公网域名 → 判为代理占位/分流并放行")
        repo_source._doh_public_verdict = lambda host: False
        await expect_error(
            check,
            lambda: validate_repo_url("https://github.com/x/y"),
            "hosted + 公网 DNS 也没有公网 A 记录 → 拒绝（真内网目标）",
            "hosted",
        )
        repo_source._doh_public_verdict = lambda host: None
        await expect_error(
            check,
            lambda: validate_repo_url("https://github.com/x/y"),
            "hosted + DoH 核验不可达 → 拒绝并给出出路",
            "DoH",
        )
        pin_resolution(("10.1.2.3", "private", "non_public"), ("140.82.113.3", "public", "public"))
        repo_source._doh_public_verdict = lambda host: True
        await expect_error(
            check,
            lambda: validate_repo_url("https://github.com/x/y"),
            "公网+非公网混合解析（rebinding 特征）→ hosted 拒绝",
            "rebinding",
        )

        # (3) 显式信任：网段白名单（企业内网镜像 / 已知 fake-ip 段）
        settings.repo_allow_cidrs = ["198.18.0.0/15"]
        pin_resolution(("198.18.0.47", "private", "non_public"))
        notes = validate_repo_url("https://github.com/x/y")   # DoH 此时仍是 None：证明放行来自显式信任
        check(
            any("显式信任" in note for note in notes),
            "hosted + PAPERLENS_REPO_ALLOW_CIDRS 显式信任的网段直接放行（不依赖 DoH）",
        )
        settings.repo_network_mode = "local"
        settings.repo_allow_cidrs = ["127.0.0.0/8"]
        pin_resolution(("127.0.0.1", "loopback", "hard"))
        notes = validate_repo_url("https://github.com/x/y")
        check(any("显式信任" in note for note in notes), "显式信任的网段覆盖硬拦（本机镜像也要能用）")
        settings.repo_allow_cidrs = ["不是网段"]
        await expect_error(
            check, lambda: validate_repo_url("https://github.com/x/y"), "ALLOW_CIDRS 写错给看得懂的报错", "看不懂"
        )
        settings.repo_allow_cidrs = []

        # (4) 代理环境变量：域名由代理解析，本机答案不代表目的地 → 跳过判定
        pin_resolution(("10.1.2.3", "private", "non_public"))
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
        try:
            notes = validate_repo_url("https://github.com/x/y")
        finally:
            del os.environ["HTTPS_PROXY"]
        check(any("HTTPS_PROXY" in note for note in notes), "检测到代理环境变量 → 跳过后端本机 DNS 判定")

        # (5) 旧逃生开关仍然可用（兼容）
        settings.repo_allow_private_ips = True
        pin_resolution(("127.0.0.1", "loopback", "hard"))
        notes = validate_repo_url("https://github.com/x/y")
        check(
            any("ALLOW_PRIVATE_IPS" in note for note in notes),
            "旧开关 PAPERLENS_REPO_ALLOW_PRIVATE_IPS=true 仍然放行一切（兼容，已标注为粗粒度）",
        )
        settings.repo_allow_private_ips = False

        # (6) 白名单支持 host:port（自建 GitLab 8443）+ 报错文案可操作
        settings.repo_allowed_hosts = ["github.com", "gitlab.com", "forge.local:8443"]
        pin_resolution(("140.82.113.3", "public", "public"))
        notes = validate_repo_url("https://forge.local:8443/x/y")
        check(isinstance(notes, list), "白名单支持 host:port（自建 GitLab 的 8443）")
        port_error = ""
        try:
            validate_repo_url("https://forge.local/x/y")
        except RepoError as exc:
            port_error = str(exc)
        check(
            "PAPERLENS_REPO_ALLOWED_HOSTS=" in port_error and "forge.local:443" in port_error,
            "未声明的端口被拒绝，文案给出可直接复制的 host:port 写法",
        )
        await expect_error(
            check,
            lambda: validate_repo_url("https://gitee.com/x/y"),
            "非白名单域名的文案给出可直接复制的覆盖方法",
            "PAPERLENS_REPO_ALLOWED_HOSTS=",
        )

        # (7) git 配置隔离：跟部署模式走（local 继承，hosted 隔离）
        settings.repo_isolate_git_config = None
        settings.repo_network_mode = "local"
        local_env = _git_env()
        check(
            "GIT_CONFIG_GLOBAL" not in local_env and "GIT_CONFIG_NOSYSTEM" not in local_env,
            "local 模式继承 git 全局/系统配置（http.proxy / http.sslCAInfo / insteadOf 才用得上）",
        )
        settings.repo_network_mode = "hosted"
        hosted_env = _git_env()
        check(
            hosted_env.get("GIT_CONFIG_GLOBAL") == "/dev/null" and hosted_env.get("GIT_CONFIG_NOSYSTEM") == "1",
            "hosted 模式隔离 git 全局配置（insteadOf 无法把白名单域名改指向别处）",
        )
        settings.repo_isolate_git_config = True
        settings.repo_network_mode = "local"
        check(
            _git_env().get("GIT_CONFIG_GLOBAL") == "/dev/null",
            "PAPERLENS_REPO_ISOLATE_GIT_CONFIG 可显式覆盖模式默认值",
        )

        # (8) DoH 响应解析（纯函数，离线）
        check(
            _doh_verdict_from_payload(
                {"Status": 0, "Answer": [{"name": "github.com", "type": 1, "data": "140.82.113.3"}]}
            )
            is True
            and _doh_verdict_from_payload({"Status": 0, "Answer": [{"type": 1, "data": "10.0.0.5"}]}) is False
            and _doh_verdict_from_payload({"Status": 3}) is False
            and _doh_verdict_from_payload("<html>502 Bad Gateway</html>") is None,
            "DoH 响应解析：公网 A → True / 只有私网 → False / NXDOMAIN → False / 垃圾响应 → None",
        )

        # (9) list 型配置容错：逗号分隔曾让后端在**导入时**直接崩（SettingsError）
        from app.config import Settings

        tolerant = Settings(
            repo_allowed_hosts="github.com,gitee.com",
            repo_allow_cidrs="198.18.0.0/15, 10.20.0.0/16",
        )
        check(
            tolerant.repo_allowed_hosts == ["github.com", "gitee.com"]
            and tolerant.repo_allow_cidrs == ["198.18.0.0/15", "10.20.0.0/16"],
            "list 型配置同时接受逗号分隔与 JSON（PAPERLENS_REPO_ALLOWED_HOSTS=a,b 不再让后端启动失败）",
        )
    finally:
        repo_source.resolve_host_addresses = real_resolve
        repo_source._doh_public_verdict = real_doh
        settings.repo_allowed_hosts = hosts_ok
        settings.repo_network_mode = mode_ok
        settings.repo_allow_cidrs = cidrs_ok
        settings.repo_allow_private_ips = legacy_ok
        settings.repo_isolate_git_config = isolate_ok
        settings.repo_dns_crosscheck = crosscheck_ok

    # 打开测试开关（下面的克隆测的是本地夹具仓库），section A 结束时恢复
    settings.allow_local_repo_paths = True
    local_notes = validate_repo_url(str(source_repo))
    check(
        any("仅测试/演示用" in note for note in local_notes),
        "本地路径（演示模式）留一条「仅测试/演示用，公网部署绝不能开」的提示",
    )
    check(True, "打开测试开关后本地路径可用（默认关闭，公网部署绝不能开）")

    # ---- 克隆 ----
    clone_dir = work / "repo"
    progress_lines: list[str] = []
    info = clone_repo(str(source_repo), clone_dir, on_progress=progress_lines.append)
    check(len(info.commit_sha) == 40, f"克隆后锁定 commit：{info.commit_sha[:12]}…")
    check(
        len(progress_lines) >= 1 and any("克隆" in line or "Cloning" in line for line in progress_lines),
        f"克隆进度经 on_progress 实时回调（{len(progress_lines)} 行，首行：{progress_lines[0][:40] if progress_lines else '无'}）",
    )
    check(info.files_total >= 6, f"统计到 {info.files_total} 个文件")
    check(info.bytes > 0, f"统计体积 {info.bytes / 1024:.1f}KB（用于体积上限护栏）")
    check(not (clone_dir / ".git" / "hooks" / "post-checkout").exists(), "没有执行任何仓库自带的 hook")
    check(
        settings.repo_clone_timeout_seconds == 300,
        f"克隆超时默认 300s（60s 对经代理的 GitHub 太紧，实测 7MB 仓库下载即超；"
        f"当前值 {settings.repo_clone_timeout_seconds}s）",
    )

    # ---- 源码视图克隆（2026-09-14）：部分克隆 + 稀疏检出 ----
    # 背景：实测 zju3dv/INTACT-JEPA 为了读 1.8MB 文本下载了 237MB（工作区 140MB 里 6 个演示
    # 视频 83MB + .git 97MB）。所以现在只取"源码视图"，并且把口径拆开如实上报。
    check(
        settings.repo_sparse is True,
        "默认启用源码视图克隆（PAPERLENS_REPO_SPARSE=true）",
    )
    patterns = sparse_exclude_patterns(clone_dir)
    check(
        patterns[0] == "/*" and any(p.startswith("!*.") for p in patterns),
        f"稀疏规则：先全包含再按后缀排除（共 {len(patterns)} 条）",
    )
    check(
        "!node_modules" in patterns and "!*.mp4" in patterns and "!*.pt" in patterns,
        "排除规则直接复用 SKIP_DIRS 与 BINARY_SUFFIXES——「不下载什么」与「工具不读什么」同一套标准",
    )
    # 仓库自己声明的二进制资产也要认（后缀表永远会漏冷门格式）
    attr_probe = work / "attr-probe"
    attr_probe.mkdir(parents=True, exist_ok=True)
    (attr_probe / ".gitattributes").write_text(
        "*.png binary\n"
        "models/*.wacky filter=lfs\n"
        "* text=auto\n"
        "* binary\n"                      # 能清空整个工作区的模式，必须被挡掉
        "!danger binary\n"
        "plainname binary\n",             # 既不带 . 也不带 /，不认
        encoding="utf-8",
    )
    attr_patterns = sparse_exclude_patterns(attr_probe)
    check(
        "!models/*.wacky" in attr_patterns and "!*.png" in attr_patterns,
        "仓库自己声明为 binary / filter=lfs 的模式被采纳（后缀表漏掉的冷门格式靠它兜底）",
    )
    check(
        len(attr_patterns) == len(set(attr_patterns)),
        f"稀疏规则去重（后缀表与 .gitattributes 会声明同一条；共 {len(attr_patterns)} 条）",
    )
    check(
        "!*" not in attr_patterns and "!plainname" not in attr_patterns,
        "危险的 .gitattributes 模式被挡掉（不可信输入不能让工作区被清空：`*`、`!…`、无路径特征的都拒绝）",
    )
    check(
        info.sparse is True and info.skipped_files >= 1,
        f"稀疏检出真的跳过了文件：夹具仓库 {info.files_tracked} 个被跟踪文件里跳过 "
        f"{info.skipped_files} 个（node_modules/junk/index.js，工具本来也不读）",
    )
    check(
        not (clone_dir / "node_modules" / "junk" / "index.js").exists(),
        "被跳过的文件确实没有落在工作区里（不是「检出了再删」）",
    )
    check(
        info.files_total == _sparse_status(clone_dir)[0] - info.skipped_files,
        f"工作区文件数与 git 的索引口径一致（{info.files_total} = {_sparse_status(clone_dir)[0]} - {info.skipped_files}）",
    )
    check(
        info.disk_bytes == info.bytes + info.git_bytes and info.git_bytes > 0,
        f"体积口径拆开且自洽：工作区 {info.bytes / 1024:.1f}KB + 对象库 {info.git_bytes / 1024:.1f}KB "
        f"= {info.disk_bytes / 1024:.1f}KB（上限管的是这个）",
    )
    check(
        info.to_dict()["git_mb"] >= 0 and info.to_dict()["disk_mb"] >= info.to_dict()["mb"],
        "上报给前端的口径：mb 是工作区内容，disk_mb 是实际占用",
    )
    check(
        any("源码视图" in note for note in info.resolution_notes),
        f"克隆策略留痕给用户看（{info.resolution_notes[-1][:50]}…）",
    )
    # 后缀表之外的冷门大二进制文件：要被点名，并给出该加的环境变量
    heavy_dir = work / "heavy-probe"
    (heavy_dir / "assets").mkdir(parents=True, exist_ok=True)
    (heavy_dir / "assets" / "coords.wacky").write_bytes(b"\x00" * (2 * 1024 * 1024))
    (heavy_dir / "src.py").write_text("print('x')\n", encoding="utf-8")
    heavy = unreadable_heavy_files(heavy_dir)
    check(
        len(heavy) == 1 and heavy[0][1] == "assets/coords.wacky",
        f"无名二进制大文件会被点名（{heavy[0][1] if heavy else '无'}），好让用户知道该排什么",
    )
    check(
        len(unreadable_heavy_files(heavy_dir, min_bytes=0)) == 1,
        "点名逻辑只挑「工具不会读」的（文本文件不会被劝排除）",
    )

    # 被拒的仓库不该留痕（原顺序：先回填缓存再量体积 → 拒绝的仓库照样占着 238MB，实测过）
    probe_src = work / "reject-probe-src"
    if not probe_src.exists():
        shutil.copytree(source_repo, probe_src)
    reject_dir = work / "reject-probe"
    limit_ok = settings.repo_max_mb
    try:
        settings.repo_max_mb = 0            # 任何内容都超标 → 必然被拒
        rejected_entry = _cache_entry(str(probe_src), resolve_head(str(probe_src)))
        if rejected_entry.exists():
            shutil.rmtree(rejected_entry, ignore_errors=True)
        try:
            clone_repo(str(probe_src), reject_dir, overwrite=True)
            check(False, "体积超限时应该报错")
        except RepoError as exc:
            check(
                "超过" in str(exc) and "工作区" in str(exc) and "对象库" in str(exc),
                f"体积超限的报错说清了口径：{str(exc)[:70]}…",
            )
        check(not rejected_entry.exists(), "被拒的仓库没有回填克隆缓存（拒绝就该不留痕）")
        check(not reject_dir.exists(), "被拒的仓库已从磁盘清理")
    finally:
        settings.repo_max_mb = limit_ok

    # ---- ls-remote 预检 + (URL, sha) 克隆缓存（2026-09-12 第一档改进）----
    prechecked = resolve_head(str(source_repo))
    check(
        prechecked == info.commit_sha,
        f"ls-remote 预检到的 HEAD 与克隆结果一致（{prechecked[:12]}…）——预检 sha 就是缓存键的一半",
    )
    settings.repo_network_mode = "local"   # 后面的端到端也按默认模式跑（判定不再误杀）
    check(
        _git_env().get("GIT_CONFIG_GLOBAL") != "/dev/null",
        "local 模式下不隔离 git 全局配置（企业代理/CA/insteadOf 是用户能克隆的前提）；"
        "hosted 模式下才隔离，见 A 段第 (7) 组断言",
    )
    check(
        info.resolution_notes
        and info.to_dict()["notes"] == info.resolution_notes
        and all(isinstance(note, str) for note in info.resolution_notes),
        f"RepoInfo 带地址判定提示并进 to_dict（{info.resolution_notes[0][:32]}…）——守卫做的决定必须留痕",
    )
    cached_info = clone_repo(str(source_repo), work / "cache-hit-clone")
    check(
        cached_info.from_cache is True and cached_info.commit_sha == info.commit_sha,
        "第二次克隆命中 (URL, sha) 缓存（本地副本秒级完成，不再走网络）",
    )
    cache_entry = _cache_entry(str(source_repo), info.commit_sha)
    check(
        cache_entry.exists(),
        "缓存条目在 data/repo-cache/<hash>/ 下（run 目录之外，clean 命令不会碰它）",
    )
    # 缓存副本必须是**硬链接复制**，不能是 `git clone`：部分克隆里"被承诺但没下载"的
    # 对象会让本地 clone 的 pack-objects 直接失败（实测 returncode=128
    # 「无法从承诺者远程获取 <oid>」），而缓存是尽力而为的 → 缓存会**无声失效**，
    # 表现为每次定位都重新下载。
    copy_probe = work / "cache-copy-probe"
    check(_copy_repo_tree(cache_entry, copy_probe), "缓存副本用复制而不是 git clone（部分克隆下后者必然失败）")
    object_files = [p for p in (cache_entry / ".git" / "objects").rglob("*") if p.is_file()]
    shared = [
        p for p in object_files
        if (copy_probe / p.relative_to(cache_entry)).exists()
        and p.stat().st_ino == (copy_probe / p.relative_to(cache_entry)).stat().st_ino
    ]
    check(
        object_files and len(shared) == len(object_files),
        f"对象库与缓存共享 inode（{len(shared)}/{len(object_files)} 个，零拷贝）",
    )
    check(
        (cache_entry / ".git" / "index").stat().st_ino != (copy_probe / ".git" / "index").stat().st_ino,
        "index 各留一份（sparse-checkout 会就地改写它，共享 inode 会让缓存与 run 互相污染）",
    )
    sparse_key = cache_entry.name
    sparse_ok = settings.repo_sparse
    try:
        settings.repo_sparse = not sparse_ok
        full_key = _cache_entry(str(source_repo), info.commit_sha).name
    finally:
        settings.repo_sparse = sparse_ok
    check(
        sparse_key != full_key,
        "缓存键含克隆策略（全量条目不会被当成源码视图复用，反之亦然）",
    )
    not_a_repo = work / "not-a-repo"
    not_a_repo.mkdir(exist_ok=True)
    await expect_error(
        check,
        lambda: clone_repo(str(not_a_repo), work / "nope"),
        "ls-remote 预检失败立刻给人话（不用等整个 clone 磁到超时）",
        "预检失败",
    )

    repo = RepoSource(clone_dir, info.commit_sha)

    # ---- 目录与搜索 ----
    tree = repo.tree("", depth=2)
    paths = {row["path"] for row in tree["rows"]}
    check("loralib/layers.py" in paths, "tree 能列出 loralib/layers.py")
    check(not any("node_modules" in path for path in paths), "tree 跳过了 node_modules（依赖不是论文的实现）")
    check(not any(path.startswith(".git") for path in paths), "tree 跳过了 .git")

    found = repo.search("lora_", glob="**/*.py")
    hit_paths = {hit["path"] for hit in found["hits"]}
    check("loralib/layers.py" in hit_paths, f"search_code('lora_') 命中 {len(found['hits'])} 处")
    check("node_modules" not in " ".join(hit_paths), "搜索跳过了 node_modules")
    truncated = repo.search("e", max_hits=3)
    check(
        truncated["complete"] is False and "截断" in truncated["note"],
        "命中数达到上限时明确标注'搜索被截断'（'没找到'≠'不存在'）",
    )

    narrow = repo.search("def build_model")
    check(narrow["complete"] is True, "扫完整个仓库时会明确说扫完了")

    # ---- 读取 ----
    forward = layer_lines("    def forward(self, x):")
    payload = repo.read_file("loralib/layers.py", forward[0], forward[1])
    check(payload["line_start"] == forward[0] and payload["line_end"] == forward[1], "read_file 支持行区间")
    check("result += after_B" in payload["numbered"], "行区间内容正确")
    check(payload["line_count_total"] > 40, f"报告了文件总行数（{payload['line_count_total']}）")

    await expect_error(
        check,
        lambda: repo.read_file("loralib/layers.py", 10_000, 10_001),
        "行区间越界被拒绝",
        "行区间非法",
    )
    await expect_error(
        check,
        lambda: repo.read_file("../outside.txt"),
        "路径穿越被拒绝",
        "越界",
    )
    await expect_error(check, lambda: repo.read_file("no/such/file.py"), "不存在的文件被拒绝", "文件不存在")

    # 二进制与超大文件：用一个独立的临时目录测（不需要 git）
    binary_dir = work / "binary"
    binary_dir.mkdir(exist_ok=True)
    (binary_dir / "weights.pt").write_bytes(b"\x00\x01\x02binary")
    binary_repo = RepoSource(binary_dir, "deadbeef")
    await expect_error(check, lambda: binary_repo.read_file("weights.pt"), "二进制文件被拒绝", "二进制")

    # ---- 核验：五种失败形态 ----
    content = repo.content_at_commit("loralib/layers.py")
    snippet = "\n".join(content.split("\n")[forward[0] - 1 : forward[1]])
    good = {
        "path": "loralib/layers.py",
        "line_start": forward[0],
        "line_end": forward[1],
        "snippet_sha256": snippet_sha256(snippet),
        "quote": quote_from_lines(*forward, "result += after_B"),
    }
    outcome = check_evidence(repo, good)
    check(outcome["state"] == "verified", f"正确引用核验通过（文件共 {outcome['line_count']} 行）")

    reset = layer_lines("    def reset_parameters(self):")
    reset_snippet = "\n".join(content.split("\n")[reset[0] - 1 : reset[1]])
    selfconsistent = {
        "path": "loralib/layers.py",
        "line_start": reset[0],
        "line_end": reset[1],
        "snippet_sha256": snippet_sha256(reset_snippet),
        "quote": None,
    }
    check(
        check_evidence(repo, selfconsistent)["state"] == "verified",
        f"另一段自洽的引用（{reset[0]}-{reset[1]} 行）同样通过：核验看内容，不认死行号",
    )
    tampered = dict(selfconsistent, line_start=forward[0], line_end=forward[1])
    outcome = check_evidence(repo, tampered)
    check(
        outcome["state"] == "failed" and any("哈希" in item for item in outcome["failures"]),
        "只改行号不改哈希 → 核验失败（防止'事后挪行号'）",
    )

    bad_hash = dict(good, snippet_sha256="0" * 64)
    outcome = check_evidence(repo, bad_hash)
    check(
        outcome["state"] == "failed" and any("哈希" in item for item in outcome["failures"]),
        "片段哈希对不上 → 核验失败（防'事后改代码骗过解读'）",
    )

    bad_quote = dict(good, quote="this text is not in those lines at all")
    outcome = check_evidence(repo, bad_quote)
    check(
        outcome["state"] == "failed" and any("没有出现" in item for item in outcome["failures"]),
        "引文不在该行区间内 → 核验失败",
    )

    outcome = check_evidence(repo, dict(good, path="loralib/ghost.py"))
    check(outcome["state"] == "failed", f"不存在的文件 → 核验失败（{outcome['failures'][0][:40]}…）")

    outcome = check_evidence(repo, dict(good, line_end=99999))
    check(outcome["state"] == "failed", "行号超出文件长度 → 核验失败")

    # ---- record_finding 的校验逻辑 ----
    bus = RunBus("unit-m2", work / "events.jsonl")
    targets = {
        "inn-1": {"name": "低秩重参数化", "one_liner": "x", "difficulty": "beginner", "paper_evidence": [], "search_hints": []},
        "inn-3": {"name": "冻结主干", "one_liner": "y", "difficulty": "beginner", "paper_evidence": [], "search_hints": []},
    }

    def fresh_ctx() -> Any:
        return ToolContext(run_id="unit-m2", run_dir=work, bus=bus, repo=repo, state={"targets": targets})

    ctx = fresh_ctx()
    await expect_error(
        check,
        lambda: RECORD_FINDING.handler({"innovation_id": "inn-9", "status": "not_found", "confidence": 0.5, "confidence_reason": "x", "not_found_reason": "y", "searched": ["z"]}, ctx),
        "提交不在清单里的 innovation_id 被打回",
        "不在本次要定位的清单里",
    )
    await expect_error(
        check,
        lambda: RECORD_FINDING.handler({"innovation_id": "inn-3", "status": "not_found", "confidence": 0.5, "confidence_reason": "x", "not_found_reason": "没实现"}, ctx),
        "报 not_found 但没写 searched 被打回",
        "必须填 searched",
    )
    await expect_error(
        check,
        lambda: RECORD_FINDING.handler({"innovation_id": "inn-3", "status": "matched", "confidence": 0.5, "confidence_reason": "x"}, ctx),
        "报 matched 却没有 code_evidence 被打回",
        "至少给一条 code_evidence",
    )

    bad_path_finding = {
        "innovation_id": "inn-1",
        "status": "matched",
        "confidence": 0.9,
        "confidence_reason": "看起来很像",
        "code_evidence": [
            {"path": "loralib/ghost.py", "line_start": 1, "line_end": 3, "why": "编的"}
        ],
    }
    await expect_error(
        check,
        lambda: RECORD_FINDING.handler(copy.deepcopy(bad_path_finding), ctx),
        "编造的代码引用第一次被打回",
        "没通过核验",
    )
    result = await RECORD_FINDING.handler(copy.deepcopy(bad_path_finding), ctx)
    stored = ctx.state["findings"][0]
    check(result.terminate is False, "record_finding 不终止运行（还有别的创新点要处理）")
    check(
        stored["code_evidence"][0]["verification"]["state"] == "failed",
        "第二次仍不合格 → 接受但标记 verification=failed",
    )

    good_finding = {
        "innovation_id": "inn-1",
        "status": "matched",
        "confidence": 0.9,
        "confidence_reason": "公式与代码逐项对得上",
        "code_evidence": [
            {
                "path": "loralib/layers.py",
                "line_start": forward[0],
                "line_end": forward[1],
                "symbol": "Linear.forward",
                "why": "低秩分支乘 scaling 加回主干",
                "quote": quote_from_lines(*forward, "result += after_B"),
            }
        ],
        "explanation": {
            "intuition": "原本要更新一整块巨大的权重矩阵，现在只用两个很窄的矩阵相乘来代替那块更新量，参数量因此少了好几个数量级。",
            "code_walkthrough": [
                {"line_ref": f"loralib/layers.py:{forward[0]}", "text": "先把主干那条路照常算出来。"},
                {"line_ref": f"loralib/layers.py:{forward[0] + 3}-{forward[1] - 1}", "text": "再算低秩分支并乘缩放系数加回主干。"},
            ],
            "pitfalls": ["别以为它在给模型加层：维度没变，推理时可以合并回原权重。"],
        },
    }
    # 解释写得太浅必须被打回（只有一句话的解释没有信息量）
    thin = {
        "innovation_id": "inn-1",
        "status": "matched",
        "confidence": 0.9,
        "confidence_reason": "看起来很像",
        "code_evidence": [
            {
                "path": "loralib/layers.py",
                "line_start": forward[0],
                "line_end": forward[1],
                "why": "低秩分支乘 scaling 加回主干",
            }
        ],
        "explanation": {"intuition": "它就是做低秩分解的。"},
    }
    thin_ctx = fresh_ctx()
    await expect_error(
        check,
        lambda: RECORD_FINDING.handler(copy.deepcopy(thin), thin_ctx),
        "只给一句没有信息量的解释 → 被打回",
        contains="解释的信息量不够",
    )
    result = await RECORD_FINDING.handler(copy.deepcopy(thin), thin_ctx)
    check(
        result.terminate is False and len(thin_ctx.state["findings"]) == 1,
        "第二次仍不合格 → 接受但已经标记（给它一次机会，不许无限重试）",
    )

    ctx2 = fresh_ctx()
    await RECORD_FINDING.handler(good_finding, ctx2)
    stored = ctx2.state["findings"][0]
    check(
        len(stored["code_evidence"][0]["snippet_sha256"]) == 64,
        "snippet_sha256 是后端读出来自己算的（模型无权提供）",
    )
    check(stored["code_evidence"][0]["verification"]["state"] == "verified", "合格引用的核验状态是 verified")
    check(stored["name"] == "低秩重参数化", "产物里带上了论文侧信息（让产物自洽）")

    not_found_finding = {
        "innovation_id": "inn-3",
        "status": "not_found",
        "confidence": 0.7,
        "confidence_reason": "搜了三种模式都没有",
        "not_found_reason": "仓库里没有冻结主干的逻辑",
        "searched": ["requires_grad", "freeze"],
        "explanation": {
            "intuition": "论文说只训练低秩分支、主干冻结，但这个仓库并没有这么做：优化器直接吃下了全部参数。",
            "pitfalls": ["看到论文写冻结就默认代码也实现了——很多复现只做了核心公式。"],
        },
    }
    await RECORD_FINDING.handler(not_found_finding, ctx2)
    check(len(ctx2.state["findings"]) == 2, "not_found 也是合格结论，会被收进产物")

    # ---- 整份产物重放 ----
    artifact = {
        "innovations": [
            {"id": "inn-1", "status": "matched", "code_evidence": [dict(good)]},
            {"id": "inn-3", "status": "not_found", "code_evidence": []},
        ]
    }
    verification = verify_artifact(repo, artifact)
    check(verification["citations_total"] == 1, "统计里只把真正的代码引用算进去（not_found 不计）")
    check(verification["citation_verifiable_rate"] == 1.0, f"引用核验率 {verification['citation_verifiable_rate']}")

    artifact["innovations"][0]["code_evidence"][0]["snippet_sha256"] = "0" * 64
    verification = verify_artifact(repo, artifact)
    check(
        verification["citation_verifiable_rate"] == 0.0 and verification["citations_failed"] == 1,
        "篡改哈希后重放核验率降到 0（重放是真的在重放）",
    )

    # ---- 行号口径统一（回归，2026-09-12）----
    # 以前 read_file 用 splitlines()，check_evidence 用 normalize_lines().split("\n")
    # （会把开头/结尾的空行整个剥掉）：文件开头/结尾有连续空行时，Agent 照工具返回的
    # 行号提交的引用会被核验误判成「行区间非法」。夹具仓库不能加这种文件
    # （会改变冻结的 commit），所以单独造一个一次性小仓库来测。
    edge_source = build_mini_repo(work / "edge-repo", {"edge.py": "\n\nvalue = 1\n\n\n"})
    edge_info = clone_repo(str(edge_source), work / "edge-clone")
    edge_repo = RepoSource(work / "edge-clone", edge_info.commit_sha)
    edge_total = edge_repo.read_file("edge.py")["line_count_total"]
    check(edge_total == 5, f"read_file 把开头/结尾的空行都算进行数（实际 {edge_total} 行）")
    edge_lines = text_lines(edge_repo.content_at_commit("edge.py"))
    edge_outcome = check_evidence(
        edge_repo,
        {
            "path": "edge.py",
            "line_start": 1,
            "line_end": edge_total,
            "snippet_sha256": snippet_sha256("\n".join(edge_lines[:edge_total])),
        },
    )
    check(
        edge_outcome["state"] == "verified" and edge_outcome["line_count"] == edge_total,
        f"核验用同一套行号口径：引用 1-{edge_total} 行通过（{edge_outcome['state']}，"
        f"核验方数出 {edge_outcome['line_count']} 行 vs read_file {edge_total} 行）",
    )

    # 恢复配置（这个进程马上就用完了，但别留脏状态）
    settings.allow_local_repo_paths = local_ok
    settings.repo_allowed_hosts = hosts_ok
    return {"clone_dir": clone_dir}


# ---------------------------------------------------------------------------
# B / C. 端到端：上传 → 侦察 → 定位
# ---------------------------------------------------------------------------
async def prepare_run(client: httpx.AsyncClient, check: Checker, model: str) -> str:
    pdf = (ROOT / "tests" / "fixtures" / "synthetic_paper.pdf").read_bytes()
    response = await client.post(
        "/api/runs",
        files={"file": ("synthetic_paper.pdf", pdf, "application/pdf")},
        data={"provider": json.dumps(provider(model))},
    )
    run_id = response.json()["run_id"]
    baseline = await current_max_event_id(client, run_id)
    started = await client.post(f"/api/runs/{run_id}/recon", json={"provider": provider(model)})
    if started.status_code != 200:
        raise RuntimeError(f"启动侦察失败：{started.text}")
    await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)
    return run_id


async def locate(
    client: httpx.AsyncClient,
    check: Checker,
    run_id: str,
    model: str,
    *,
    selected_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    repo_url = str(build_repo(ROOT))
    # 只收集本阶段新产生的事件：事件流会重放历史，里面带着上一阶段的 run_end
    baseline = await current_max_event_id(client, run_id)
    payload: dict[str, Any] = {"provider": provider(model), "repo_url": repo_url}
    if selected_ids is not None:
        payload["selected_ids"] = selected_ids
    response = await client.post(f"/api/runs/{run_id}/locate", json=payload)
    check(response.status_code == 200, f"阶段 B 已启动（HTTP {response.status_code} {response.text[:120]}）")
    events, first_at, end_at = await collect_sse(
        client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline
    )
    print(f"   事件序列：{' → '.join(e['type'] for e in events)}", flush=True)
    return events


async def section_b(check: Checker, client: httpx.AsyncClient) -> str:
    check.section("B. 端到端：上传 → 侦察 → 定位 → 核验")
    run_id = await prepare_run(client, check, "mock-model")
    events = await locate(client, check, run_id, "mock-model")
    types = [event["type"] for event in events]

    check("repo_cloning" in types and "repo_ready" in types, "克隆阶段有明确的事件（用户看得见在干什么）")
    check(
        "clone_progress" in types,
        f"克隆进度实时转发为 clone_progress 事件（共 {types.count('clone_progress')} 条，慢下载不再是黑盒）",
    )
    repo_ready = events_of(events, "repo_ready")[0]["data"]["repo"]
    check(len(repo_ready["commit_sha"]) == 40, f"产物锁定了 commit：{repo_ready['commit_sha'][:12]}…")
    # 守卫的判定必须一路走到用户眼前（RepoInfo → to_dict → repo_ready 事件 → 前端时间线）：
    # 夹具走的是本地路径，正好会带出"仅测试/演示用"的提示。
    check(
        isinstance(repo_ready.get("notes"), list) and repo_ready["notes"],
        f"地址判定提示随 repo_ready 事件到达前端（{str(repo_ready.get('notes'))[:60]}…）",
    )

    calls = [event["data"]["tool"] for event in events_of(events, "tool_call")]
    check(
        calls[:3] == ["repo_tree", "search_code", "read_file"],
        f"先摸结构、再搜、再精读：{calls[:3]}",
    )
    check(calls.count("record_finding") == 3, f"每条创新点各提交一次：{calls.count('record_finding')} 次")
    check(calls[-1] == "finish", "最后调用 finish 收尾")

    findings = events_of(events, "finding")
    statuses = {event["data"]["innovation_id"]: event["data"]["status"] for event in findings}
    check(
        statuses.get("inn-1") == "matched" and statuses.get("inn-2") == "matched",
        f"两条找到实现：{statuses}",
    )
    check(statuses.get("inn-3") == "not_found", "一条诚实报告找不到（仓库里确实没实现冻结主干）")

    done = events_of(events, "verification_done")
    check(len(done) == 1, "收到 verification_done 事件")
    payload = done[0]["data"]["summary"]
    check(payload["citations_total"] == 2, f"产物里有 2 条代码引用（实际 {payload['citations_total']}）")
    check(
        payload["citation_verifiable_rate"] == 1.0,
        f"**引用核验率 {payload['citation_verifiable_rate']}**（头号指标）",
    )
    check(payload["status_counts"].get("not_found") == 1, "统计里如实包含 not_found")
    check(done[0]["data"]["missing_ids"] == [], "没有漏掉任何一条创新点")

    artifact_path = ROOT / "data" / run_id / "artifact.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    check(artifact["run"]["repo"]["commit_sha"] == repo_ready["commit_sha"], "产物里的 commit 与克隆时一致")
    check(artifact["run"]["paper"]["page_count"] == 6, "产物里带上了论文信息")
    check(artifact["not_found"][0]["searched"], f"not_found 条目保留了搜索记录：{artifact['not_found'][0]['searched'][:2]}")
    first = artifact["innovations"][0]["code_evidence"][0]
    check(
        {"path", "line_start", "line_end", "snippet_sha256", "verification"} <= set(first),
        "每条引用都带 path/行号/片段哈希/核验状态（缺一不可）",
    )
    check(first["verification"]["state"] == "verified", "交付前又被正式核验了一遍")

    # ---- 机械重放：拿产物再核验一次，结果必须一致 ----
    from app.repo_source import RepoSource
    from app.verify import verify_artifact

    repo = RepoSource(ROOT / "data" / run_id / "repo", artifact["run"]["repo"]["commit_sha"])
    replay = verify_artifact(repo, copy.deepcopy(artifact))
    check(
        replay["citation_verifiable_rate"] == artifact["verification"]["citation_verifiable_rate"],
        f"用 git 对象重放核验，结果一致（{replay['citation_verifiable_rate']}）—— 解读不会随时间腐烂",
    )

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    check(
        (detail["artifact"] or {}).get("innovations") is not None,
        "GET /api/runs/{id} 能拿到落盘的产物（刷新页面不丢）",
    )
    return run_id


async def section_h(check: Checker, client: httpx.AsyncClient, run_id: str) -> None:
    """回归（2026-09-12）：同一个 run 第二次点「开始定位」必须能跑通。

    之前 clone_repo 看到 repo/ 已存在就直接报「目标目录已存在」——而换勾选、
    划选补目标、失败重试都会触发第二次定位，用户被 UI 引导着走进这个死胡同。
    修复后定位是可重入的：repo/ 是派生数据，覆盖重克隆即可。
    """
    check.section("H. 重复定位：同一个 run 再跑一次阶段 B")
    events = await locate(client, check, run_id, "mock-model", selected_ids=["inn-2"])
    types = [event["type"] for event in events]
    end = events_of(events, "run_end")[0]["data"]
    check(end["status"] == "ok", f"第二次定位正常结束（{end['stopped_reason'][:60]}）")
    check(
        "repo_cloning" in types and "repo_ready" in types,
        "第二次定位重新克隆了仓库（不撞「目标目录已存在」）",
    )
    done = events_of(events, "verification_done")
    check(
        len(done) == 1 and done[0]["data"]["summary"]["citations_total"] == 1,
        "第二次的产物按本次勾选重新核验（1 条引用）",
    )
    detail = (await client.get(f"/api/runs/{run_id}")).json()
    artifact = detail["artifact"]
    check(
        [item["id"] for item in artifact["innovations"]] == ["inn-2"],
        "产物被第二次定位覆盖为本次勾选的内容",
    )


async def section_c(check: Checker, client: httpx.AsyncClient) -> None:
    check.section("C. 诚实性机制：编造的代码引用会被打回")
    run_id = await prepare_run(client, check, "bad-evidence")
    events = await locate(client, check, run_id, "bad-evidence")

    results = events_of(events, "tool_result")
    finding_results = [item for item in results if item["data"]["tool"] == "record_finding"]
    check(len(finding_results) >= 4, f"record_finding 被调用了 {len(finding_results)} 次（含一次被打回）")
    first_error = finding_results[0]
    check(first_error["data"]["is_error"] is True, "第一次提交被标记为错误")
    check(
        "没通过核验" in first_error["data"]["summary"],
        f"打回信息说明了原因：{first_error['data']['summary'][:90]}",
    )
    payload = events_of(events, "verification_done")[0]["data"]["summary"]
    check(payload["citation_verifiable_rate"] == 1.0, "修正后的引用全部通过核验")


async def section_e(check: Checker, client: httpx.AsyncClient) -> None:
    """只勾选一部分创新点时，不该出现"反复被拒"的空转。"""
    check.section("E. 只勾选部分创新点")
    run_id = await prepare_run(client, check, "mock-model")
    events = await locate(client, check, run_id, "mock-model", selected_ids=["inn-1"])

    calls = [event["data"]["tool"] for event in events_of(events, "tool_call")]
    errors = [
        item for item in events_of(events, "tool_result") if item["data"]["is_error"]
    ]
    check(calls.count("record_finding") == 1, f"只提交被勾选的那一条（record_finding {calls.count('record_finding')} 次）")
    check(not errors, f"没有出现被拒的调用（{len(errors)} 次错误）")
    check(calls[-1] == "finish", "提交完就 finish，不是硬塞其余几条")

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    artifact = detail["artifact"]
    check(len(artifact["innovations"]) == 1, f"产物里只有勾选的那条（{len(artifact['innovations'])} 条）")
    check(artifact["missing_ids"] == [], "没有'漏掉'的条目——没勾的不算漏")
    check(detail["summary"]["status"] == "ok", f"运行正常结束（{detail['summary']['stopped_reason']}）")


async def section_g(check: Checker, client: httpx.AsyncClient) -> None:
    """解释质量门槛的端到端验证：太浅会被打回重写。"""
    check.section("G. 解释写得太浅会被打回重写")
    run_id = await prepare_run(client, check, "thin-explanation")
    events = await locate(client, check, run_id, "thin-explanation")

    results = [item for item in events_of(events, "tool_result") if item["data"]["tool"] == "record_finding"]
    check(len(results) >= 2, f"record_finding 被调用了 {len(results)} 次（含一次被打回）")
    check(results[0]["data"]["is_error"] is True, "第一次提交被标记为错误")
    check(
        "解释的信息量不够" in results[0]["data"]["summary"],
        f"打回的理由说清了是解释太浅：{results[0]['data']['summary'][:100]}",
    )

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    artifact = detail["artifact"]
    check(artifact["verification"]["citation_verifiable_rate"] == 1.0, "重写后的引用仍然通过核验")


async def section_f(check: Checker, client: httpx.AsyncClient) -> None:
    """模型卡在同一个错误上时，必须提前停止而不是空转到上限。"""
    check.section("F. 原地打转要早停")
    run_id = await prepare_run(client, check, "stuck-model")
    events = await locate(client, check, run_id, "stuck-model")

    calls = events_of(events, "tool_call")
    end = events_of(events, "run_end")[0]["data"]
    check(
        end["usage"]["tool_calls"] <= 6,
        f"只烧了 {end['usage']['tool_calls']} 次工具调用就停了（否则会一路到 40 次上限）",
    )
    check(
        "同一个工具错误" in end["stopped_reason"],
        f"停止原因说清了是原地打转：{end['stopped_reason'][:80]}",
    )
    warnings = events_of(events, "budget_warning")
    check(len(warnings) >= 1, "发了 budget_warning 事件，用户能看见为什么停")


# ---------------------------------------------------------------------------
# D. 失败路径
# ---------------------------------------------------------------------------
async def section_i(check: Checker, client: httpx.AsyncClient) -> None:
    """限流导致中途失败时，**已定位到的结论必须照样交付**（2026-09-15 用户实测）。

    用户那次跑了 20 轮（35 次工具调用、61.6s）撞上网关限额，run 直接 failed —— 前面
    19 轮定位到的东西全丢了，等于"一次完整的体验都没有"。这一节钉住：
    失败路径也要走 finalize（核验 + 落盘 + verification_done），并且如实标注 partial。
    """
    from app import providers as providers_module
    from app.config import settings

    check.section("I. 网关限流导致失败时：已确认的部分照样交付")

    providers_module.reset_llm_pacing()
    saved_retries = settings.llm_max_retries
    try:
        settings.llm_max_retries = 1                  # 断言要快：退避一次就放弃
        # 侦察用普通 mock（先拿到清单），定位用限流变体：它在第 4 轮工具结果之后一直 429
        # —— 那一刻第一条结论已经 record_finding 过了，正好检验"交付已确认的部分"。
        run_id = await prepare_run(client, check, "mock-model")
        events = await locate(client, check, run_id, "rate-limited-late-4")
        types = [event["type"] for event in events]
        end = events_of(events, "run_end")
        check(bool(end), "持续限流最终以 run_end 收尾（不会挂住）")
        if end:
            summary = end[0]["data"]
            check(summary["status"] == "failed", f"如实标记失败（status={summary['status']}）")
            check(
                "限流" in str(summary.get("stopped_reason", "")),
                f"失败原因指得出是限流：{str(summary.get('stopped_reason'))[:80]}…",
            )
            check(summary.get("partial") is True, "标记为 partial（前端能区分'跑完'与'只交付一部分'）")
        # 关键三连：核验事件、产物、引用状态——失败不该让它们消失
        check("verification_done" in types, "失败路径也发了 verification_done（已确认的引用被核验过）")
        check("error" in types, "错误事件在 run_end 之前发出（用户看得见失败原因）")
        detail = await client.get(f"/api/runs/{run_id}")
        artifact = detail.json().get("artifact") or {}
        findings = artifact.get("innovations") or []
        check(
            bool(findings),
            f"失败时已定位到的结论仍然交付（{len(findings)} 条，不是一片空白）",
        )
        check(
            isinstance(artifact.get("verification"), dict),
            "交付的产物带核验结果（不是「没验过就给你」）",
        )
    finally:
        settings.llm_max_retries = saved_retries
        providers_module.reset_llm_pacing()
async def section_d(check: Checker, prepared_run_id: str) -> None:
    check.section("D. 失败路径")
    from app.config import settings
    from app.main import app
    from app.repo_source import RepoError, clone_repo

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as client:
        pdf = (ROOT / "tests" / "fixtures" / "synthetic_paper.pdf").read_bytes()
        created = await client.post(
            "/api/runs",
            files={"file": ("p.pdf", pdf, "application/pdf")},
            data={"provider": json.dumps(provider("mock-model"))},
        )
        fresh_run_id = created.json()["run_id"]

        response = await client.post(
            f"/api/runs/{fresh_run_id}/locate",
            json={"provider": provider("mock-model"), "repo_url": "https://github.com/foo/bar"},
        )
        check(response.status_code == 409, f"还没跑侦察就定位 → 409（HTTP {response.status_code}）")

        # 换一个有清单的 run，才测得到地址白名单这一关
        response = await client.post(
            f"/api/runs/{prepared_run_id}/locate",
            json={"provider": provider("mock-model"), "repo_url": "https://bitbucket.org/foo/bar"},
        )
        check(response.status_code == 422, f"非白名单仓库地址 → 422（HTTP {response.status_code}）")

        response = await client.post(
            f"/api/runs/{prepared_run_id}/locate",
            json={
                "provider": provider("mock-model"),
                "repo_url": str(build_repo(ROOT)),
                "selected_ids": ["nope-1"],
            },
        )
        check(response.status_code == 422, f"勾选的 id 对不上清单 → 422（HTTP {response.status_code}）")

        response = await client.post("/api/runs/no-such-run/locate", json={"provider": provider("mock-model"), "repo_url": "https://github.com/foo/bar"})
        check(response.status_code == 404, f"未知 run → 404（HTTP {response.status_code}）")

    # 克隆失败（仓库不存在）应当转成 RepoError 而不是裸的异常
    target = ROOT / "data" / f"clone-fail-{int(time.time())}"
    shutil.rmtree(target, ignore_errors=True)
    try:
        clone_repo("https://github.com/earendil-works/this-repo-does-not-exist-xyz", target)
        check(False, "不存在的仓库应该报错")
    except RepoError as exc:
        check(True, f"不存在的仓库 → RepoError：{str(exc)[:90]}")
    except Exception as exc:  # noqa: BLE001
        check(False, f"抛了非预期异常 {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(target, ignore_errors=True)


async def main() -> int:
    check = Checker("M2 验收")
    mock = start_service("devtools.mock_provider:app", MOCK_PORT)
    app_proc = start_service(
        "app.main:app",
        APP_PORT,
        extra_env={"PAPERLENS_ALLOW_LOCAL_REPO_PATHS": "true"},  # 本地夹具仓库需要这个开关
    )
    try:
        await wait_http(f"http://127.0.0.1:{MOCK_PORT}/health")
        await wait_http(f"{APP}/api/health")
        print(f"mock provider : {MOCK_PORT}\nPaperLens API : {APP}", flush=True)

        await section_a(check)

        prepared_run_id = ""
        async with httpx.AsyncClient(base_url=APP, timeout=300) as client:
            prepared_run_id = await section_b(check, client)
            await section_h(check, client, prepared_run_id)
            await section_c(check, client)
            await section_e(check, client)
            await section_f(check, client)
            await section_g(check, client)
            await section_i(check, client)

        await section_d(check, prepared_run_id)
        return check.finish()
    finally:
        stop_services(mock, app_proc)
        from app.config import settings

        settings.allow_local_repo_paths = False


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
