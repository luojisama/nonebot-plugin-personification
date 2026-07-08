from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any


_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_GIT_TIMEOUT_SECONDS = 60.0
_LOG_FORMAT = "%H%x1f%h%x1f%ct%x1f%an%x1f%s"


def default_plugin_root() -> Path:
    return _PLUGIN_ROOT


def _as_root(path: str | Path | None) -> Path:
    return Path(path or _PLUGIN_ROOT).resolve()


def _short_hash(value: str) -> str:
    raw = str(value or "").strip()
    return raw[:7] if raw else ""


def _redact_remote_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    return re.sub(r"^(https?://)([^/@]+@)", r"\1***@", raw)


def normalize_git_remote_url(url: str) -> str:
    raw = str(url or "").strip()
    match = re.match(r"^git@([^:]+):(.+?)(\.git)?$", raw)
    if match:
        host, path = match.group(1), match.group(2)
        return f"https://{host}/{path}.git"
    return raw


def _mirror_prefixes(plugin_config: Any) -> list[str]:
    plural = getattr(plugin_config, "personification_git_mirror_prefixes", None) or []
    if isinstance(plural, str):
        plural = [item.strip() for item in plural.split(",")]
    singular = str(getattr(plugin_config, "personification_git_mirror_prefix", "") or "").strip()
    out: list[str] = []
    seen: set[str] = set()
    for item in list(plural or []) + [singular]:
        value = str(item or "").strip().rstrip("/")
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _looks_like_network_failure(stderr: str) -> bool:
    text = str(stderr or "").lower()
    return any(
        keyword in text
        for keyword in (
            "gnutls_handshake",
            "ssl_read",
            "ssl_connect",
            "tls",
            "http2",
            "framing layer",
            "curl 16",
            "flush packet",
            "could not resolve host",
            "couldn't connect",
            "connection refused",
            "connection reset",
            "connection timed out",
            "operation timed out",
            "timed out",
            "timeout",
            "unable to access",
            "failed to connect",
            "early eof",
            "rpc failed",
            "git 命令超时",
            "超时",
            "网络",
            "无法访问",
        )
    )


async def _run_git_command(
    args: list[str],
    *,
    cwd: str,
    extra_config: list[str] | None = None,
    timeout: float = _GIT_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    cmd: list[str] = []
    for item in extra_config or []:
        cmd.extend(["-c", item])
    cmd.extend(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max(1.0, float(timeout or 1.0)))
    except asyncio.TimeoutError:
        return -1, "", f"git 命令超时（{int(timeout)}s）"
    except FileNotFoundError:
        return -1, "", "找不到 git 命令，请确认已安装 git"
    except Exception as exc:
        return -1, "", str(exc)
    return (
        int(proc.returncode or 0),
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def _origin_https_url(cwd: str, remote_name: str = "origin") -> str:
    rc, out, _err = await _run_git_command(["remote", "get-url", remote_name or "origin"], cwd=cwd)
    if rc != 0:
        return ""
    return normalize_git_remote_url(out)


async def _probe_mirror(mirror: str, repo_url: str, *, timeout: float = 4.0) -> bool:
    target = f"{mirror}/{repo_url}" if mirror else repo_url

    def _do_probe() -> bool:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            target,
            method="HEAD",
            headers={"User-Agent": "personification-plugin-update"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                code = int(getattr(response, "status", 200) or 200)
                return 0 < code < 500
        except urllib.error.HTTPError as exc:
            return 0 < int(exc.code or 0) < 500
        except Exception:
            return False

    try:
        return await asyncio.wait_for(asyncio.to_thread(_do_probe), timeout=timeout + 1.0)
    except Exception:
        return False


async def _run_git_with_mirror_fallback(
    args: list[str],
    *,
    cwd: str,
    plugin_config: Any = None,
    remote_name: str = "origin",
) -> tuple[int, str, str, str, list[dict[str, Any]]]:
    mirrors = _mirror_prefixes(plugin_config)
    probe_log: list[dict[str, Any]] = []

    if mirrors:
        repo_url = await _origin_https_url(cwd, remote_name=remote_name)
        if repo_url:
            probes = await asyncio.gather(
                *(_probe_mirror(mirror, repo_url) for mirror in mirrors),
                return_exceptions=True,
            )
            for mirror, result in zip(mirrors, probes):
                ok = result is True
                probe_log.append({"mirror": mirror, "ok": ok})

            for probe in probe_log:
                if not probe.get("ok"):
                    continue
                mirror = str(probe.get("mirror") or "")
                rewrite = f"url.{mirror}/https://github.com/.insteadOf=https://github.com/"
                rc, out, err = await _run_git_command(args, cwd=cwd, extra_config=[rewrite])
                if rc == 0:
                    return rc, out, err, mirror, probe_log
                if not _looks_like_network_failure(err):
                    return rc, out, err, mirror, probe_log

    rc, out, err = await _run_git_command(args, cwd=cwd)
    return rc, out, err, "", probe_log


def _parse_commit_log(raw: str) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    for line in str(raw or "").splitlines():
        parts = line.split("\x1f", 4)
        if len(parts) != 5:
            continue
        full_hash, short_hash, timestamp, author, subject = parts
        try:
            ts = int(timestamp)
        except Exception:
            ts = 0
        commits.append(
            {
                "hash": full_hash,
                "short_hash": short_hash,
                "timestamp": ts,
                "author": author,
                "subject": subject,
            }
        )
    return commits


async def _git_log(cwd: str, rev_range: str, *, limit: int) -> list[dict[str, Any]]:
    args = ["log", f"--max-count={max(1, min(int(limit or 20), 100))}", f"--pretty=format:{_LOG_FORMAT}"]
    if rev_range:
        args.append(rev_range)
    rc, out, _err = await _run_git_command(args, cwd=cwd)
    if rc != 0:
        return []
    return _parse_commit_log(out)


def _parse_ahead_behind(value: str) -> tuple[int, int]:
    parts = str(value or "").split()
    if len(parts) < 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except Exception:
        return 0, 0


async def _resolve_git_context(plugin_root: Path) -> tuple[bool, dict[str, Any]]:
    rc, out, err = await _run_git_command(["rev-parse", "--show-toplevel"], cwd=str(plugin_root))
    if rc != 0:
        return False, {
            "available": False,
            "source_type": "unknown",
            "update_supported": False,
            "plugin_root": str(plugin_root),
            "message": f"当前安装目录不是 Git 仓库：{err or out or '无法解析仓库根目录'}",
        }
    repo_root = Path(out).resolve()
    try:
        plugin_subdir = str(plugin_root.relative_to(repo_root)).replace("\\", "/") or "."
    except Exception:
        plugin_subdir = ""
    return True, {
        "available": True,
        "source_type": "git",
        "update_supported": True,
        "plugin_root": str(plugin_root),
        "repo_root": str(repo_root),
        "plugin_subdir": plugin_subdir,
    }


async def get_plugin_update_status(
    *,
    plugin_root: str | Path | None = None,
    plugin_config: Any = None,
    refresh: bool = False,
    history_limit: int = 12,
) -> dict[str, Any]:
    root = _as_root(plugin_root)
    ok, context = await _resolve_git_context(root)
    if not ok:
        context["checked_at"] = time.time()
        return context

    cwd = context["repo_root"]
    fetch_info = {"attempted": False, "ok": True, "error": "", "used_mirror": "", "probes": []}

    rc_branch, branch, _branch_err = await _run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if rc_branch != 0:
        branch = ""

    rc_upstream, upstream, upstream_err = await _run_git_command(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=cwd,
    )
    if rc_upstream != 0:
        upstream = ""

    remote_name = "origin"
    if upstream and "/" in upstream:
        remote_name = upstream.split("/", 1)[0]
    remote_url_raw = await _origin_https_url(cwd, remote_name=remote_name)

    if refresh:
        fetch_info["attempted"] = True
        rc_fetch, fetch_out, fetch_err, used_mirror, probes = await _run_git_with_mirror_fallback(
            ["fetch", "--prune"],
            cwd=cwd,
            plugin_config=plugin_config,
            remote_name=remote_name,
        )
        fetch_info.update(
            {
                "ok": rc_fetch == 0,
                "error": "" if rc_fetch == 0 else (fetch_err or fetch_out or "fetch 失败"),
                "used_mirror": used_mirror,
                "probes": probes,
            }
        )

    rc_local, local_hash, local_err = await _run_git_command(["rev-parse", "HEAD"], cwd=cwd)
    if rc_local != 0:
        return {
            **context,
            "checked_at": time.time(),
            "source": {"type": "git", "remote_name": remote_name, "remote_url": _redact_remote_url(remote_url_raw)},
            "fetch": fetch_info,
            "update_available": False,
            "message": f"无法读取本地版本：{local_err}",
        }

    remote_hash = ""
    remote_error = ""
    if upstream:
        rc_remote, remote_hash, remote_err = await _run_git_command(["rev-parse", "@{u}"], cwd=cwd)
        if rc_remote != 0:
            remote_error = remote_err
            remote_hash = ""
    else:
        remote_error = upstream_err or "当前分支未配置 upstream"

    ahead = behind = 0
    if upstream:
        rc_ab, ab_out, _ab_err = await _run_git_command(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=cwd)
        if rc_ab == 0:
            ahead, behind = _parse_ahead_behind(ab_out)

    rc_dirty, dirty_out, _dirty_err = await _run_git_command(["status", "--porcelain"], cwd=cwd)
    dirty_lines = [line for line in str(dirty_out or "").splitlines() if line.strip()] if rc_dirty == 0 else []

    history = await _git_log(cwd, "", limit=history_limit)
    pending_history = await _git_log(cwd, "HEAD..@{u}", limit=history_limit) if upstream else []
    update_available = bool(upstream and behind > 0)
    if remote_hash and local_hash and local_hash != remote_hash and behind == 0 and ahead == 0:
        update_available = True

    message = "已是最新版本"
    if not fetch_info["ok"]:
        message = f"远端检查失败：{fetch_info['error']}"
    elif remote_error:
        message = f"无法判断远端版本：{remote_error}"
    elif update_available:
        message = f"发现 {behind or len(pending_history) or 1} 个待更新提交"
    elif ahead > 0:
        message = f"本地领先 upstream {ahead} 个提交"
    elif dirty_lines:
        message = "本地有未提交改动"

    return {
        **context,
        "checked_at": time.time(),
        "source": {
            "type": "git",
            "remote_name": remote_name,
            "remote_url": _redact_remote_url(remote_url_raw),
            "branch": branch,
            "upstream": upstream,
        },
        "local": {"hash": local_hash, "short_hash": _short_hash(local_hash), "branch": branch},
        "remote": {
            "hash": remote_hash,
            "short_hash": _short_hash(remote_hash),
            "upstream": upstream,
            "error": remote_error,
        },
        "ahead": ahead,
        "behind": behind,
        "update_available": update_available,
        "dirty": bool(dirty_lines),
        "dirty_count": len(dirty_lines),
        "dirty_preview": dirty_lines[:12],
        "fetch": fetch_info,
        "history": history,
        "pending_history": pending_history,
        "message": message,
    }


async def get_plugin_update_history(
    *,
    plugin_root: str | Path | None = None,
    plugin_config: Any = None,
    limit: int = 30,
    refresh: bool = False,
) -> dict[str, Any]:
    status = await get_plugin_update_status(
        plugin_root=plugin_root,
        plugin_config=plugin_config,
        refresh=refresh,
        history_limit=limit,
    )
    return {
        "available": bool(status.get("available")),
        "source_type": status.get("source_type", "unknown"),
        "source": status.get("source", {}),
        "history": status.get("history", []),
        "pending_history": status.get("pending_history", []),
        "fetch": status.get("fetch", {}),
        "message": status.get("message", ""),
    }


async def perform_plugin_update(
    *,
    plugin_root: str | Path | None = None,
    plugin_config: Any = None,
) -> dict[str, Any]:
    before = await get_plugin_update_status(
        plugin_root=plugin_root,
        plugin_config=plugin_config,
        refresh=True,
        history_limit=20,
    )
    if not before.get("update_supported"):
        return {"ok": False, "updated": False, "status": before, "error": before.get("message") or "当前安装源不支持自动更新"}
    if before.get("dirty"):
        return {
            "ok": False,
            "updated": False,
            "status": before,
            "error": "本地有未提交改动，已拒绝自动更新；请先提交、暂存或手动处理这些改动。",
        }
    if not before.get("update_available"):
        return {"ok": True, "updated": False, "status": before, "message": before.get("message") or "已是最新版本"}

    source = before.get("source") or {}
    remote_name = str(source.get("remote_name") or "origin")
    cwd = str(before.get("repo_root") or "")
    rc, out, err, used_mirror, probes = await _run_git_with_mirror_fallback(
        ["pull", "--ff-only"],
        cwd=cwd,
        plugin_config=plugin_config,
        remote_name=remote_name,
    )
    if rc != 0:
        return {
            "ok": False,
            "updated": False,
            "status": before,
            "error": err or out or "pull 失败",
            "pull": {"ok": False, "output": out, "error": err, "used_mirror": used_mirror, "probes": probes},
        }

    after = await get_plugin_update_status(
        plugin_root=plugin_root,
        plugin_config=plugin_config,
        refresh=False,
        history_limit=20,
    )
    return {
        "ok": True,
        "updated": True,
        "before": before,
        "status": after,
        "message": out or "已更新",
        "pull": {"ok": True, "output": out, "error": "", "used_mirror": used_mirror, "probes": probes},
    }


__all__ = [
    "default_plugin_root",
    "get_plugin_update_history",
    "get_plugin_update_status",
    "normalize_git_remote_url",
    "perform_plugin_update",
]
