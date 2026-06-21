"""诊断 gemini-cli OAuth 凭证状态。

用法：
    python -m plugin.personification.scripts.diag_gemini_oauth
    python -m plugin.personification.scripts.diag_gemini_oauth --refresh

会做：
    1. 找到 oauth_creds.json
    2. 显示 access_token / refresh_token 是否存在
    3. 显示 expiry 距今多少分钟
    4. 可选：直接调 Google oauth2.googleapis.com/token 测试 refresh
    5. 可选：调 loadCodeAssist 验证 token 是否对端有效
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


sys.path.insert(0, str(_project_root()))


from plugin.personification.skills.skillpacks.tool_caller.scripts import impl  # noqa: E402


def _human_expiry(expiry_ms: int) -> str:
    if expiry_ms <= 0:
        return "(未知)"
    remain = expiry_ms - int(time.time() * 1000)
    minutes = remain / 60_000
    ts = datetime.fromtimestamp(expiry_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    if remain <= 0:
        return f"已过期 {ts}（{-minutes:.1f} 分钟前）"
    return f"{ts}（剩 {minutes:.1f} 分钟）"


async def _run(args: argparse.Namespace) -> int:
    auth_file, searched = impl._find_gemini_cli_auth_file_with_log(args.auth_path or "")
    if auth_file is None:
        print("[FAIL] 未找到 oauth_creds.json")
        print("       搜索过的路径:")
        for path in searched:
            print(f"         - {path}")
        print("       建议：在本机执行 `gemini auth login` 完成 Google OAuth 登录。")
        return 2

    print(f"[OK] 找到 auth file: {auth_file}")
    auth = impl._load_gemini_cli_auth(auth_file)
    access = impl._get_gemini_cli_access_token(auth)
    refresh = impl._get_gemini_cli_refresh_token(auth)
    expiry_ms = impl._get_gemini_cli_token_expiry_ms(auth)

    print(f"     access_token: {'有 (' + access[:14] + '…)' if access else '缺失'}")
    print(f"     refresh_token: {'有 (' + refresh[:14] + '…)' if refresh else '缺失'}")
    print(f"     expiry: {_human_expiry(expiry_ms)}")

    if not refresh:
        print("[WARN] refresh_token 缺失，无法自动续期；请重新 `gemini auth login`。")

    if args.refresh:
        if not refresh:
            print("[SKIP] --refresh 无效：refresh_token 缺失。")
        else:
            print("\n[TEST] 调 https://oauth2.googleapis.com/token 测试 refresh…")
            try:
                payload = await impl._refresh_gemini_cli_access_token(refresh)
                new_token = str(payload.get("access_token", "") or "")
                expires_in = int(payload.get("expires_in", 0) or 0)
                if new_token:
                    print(f"[OK] refresh 成功，新 access_token={new_token[:14]}… expires_in={expires_in}s")
                    if args.write:
                        impl._persist_refreshed_gemini_cli_auth(
                            auth_file, auth,
                            access_token=new_token,
                            expires_in=expires_in,
                            id_token=str(payload.get("id_token", "") or ""),
                        )
                        print(f"[OK] 已写回 {auth_file}")
                    else:
                        print("     （未写回。加 --write 参数会更新 oauth_creds.json）")
                    access = new_token
                else:
                    print("[FAIL] refresh 响应中 access_token 为空")
                    return 3
            except Exception as exc:
                print(f"[FAIL] refresh 失败: {exc}")
                print("       原因可能：")
                print("         - refresh_token 已被 Google 撤销（超 6 个月未用 / 用户在 Google 账户里撤销了）")
                print("         - 网络无法访问 oauth2.googleapis.com")
                print("       修复：在本机执行 `gemini auth login` 重新登录。")
                return 3

    if args.load_code_assist:
        if not access:
            print("[SKIP] --load-code-assist 无效：access_token 缺失。")
        else:
            print("\n[TEST] 调 loadCodeAssist 验证 token 对端有效…")
            try:
                import httpx
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                    resp = await client.post(
                        impl._GEMINI_CLI_LOAD_CODE_ASSIST,
                        json={"metadata": dict(impl._GEMINI_CLI_CLIENT_METADATA)},
                        headers=impl._gemini_cli_headers(access),
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    project = str(data.get("cloudaicompanionProject", "") or "")
                    print(f"[OK] loadCodeAssist 200; cloudaicompanionProject={project or '(空)'}")
                else:
                    body = ""
                    try:
                        body = resp.text[:400]
                    except Exception:
                        pass
                    print(f"[FAIL] loadCodeAssist HTTP {resp.status_code}: {body}")
                    if resp.status_code == 401:
                        print("       access_token 已被 Google 撤销或失效；尝试 --refresh 自动续期。")
                    return 4
            except Exception as exc:
                print(f"[FAIL] loadCodeAssist 调用异常: {exc}")
                return 4

    print("\n诊断结束。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="诊断 gemini-cli OAuth 凭证")
    parser.add_argument("--auth-path", default="", help="oauth_creds.json 路径（不填则自动搜索）")
    parser.add_argument("--refresh", action="store_true", help="实际调 Google 测试 refresh_token")
    parser.add_argument("--write", action="store_true", help="将刷新得到的新 access_token 写回 oauth_creds.json")
    parser.add_argument("--load-code-assist", action="store_true", help="测试 access_token 在 cloudcode-pa 端是否有效")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
