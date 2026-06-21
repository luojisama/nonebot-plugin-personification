#!/usr/bin/env python3
"""诊断 api_pools 在 runtime_config.json 与 env.json 中的持久化状态。

两份文件都可能持久化 personification_api_pools；启动加载顺序会先用 env.json
覆盖，再用 runtime_config.json 覆盖。.env 显式声明的字段应当被两者跳过。
"""
import json
import os


def _find_first_existing(candidates: list[str]) -> str | None:
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _print_api_pools(label: str, payload: dict, key_path: list[str]) -> None:
    cur = payload
    for k in key_path:
        if not isinstance(cur, dict):
            cur = None
            break
        cur = cur.get(k)
    print(f"\n=== {label} ===")
    if cur is None:
        print(f"  {' / '.join(key_path)} 不存在 → 该文件不影响 api_pools")
        return
    if isinstance(cur, list):
        print(f"  类型: list  长度: {len(cur)}")
        for item in cur:
            if isinstance(item, dict):
                print(f"    - name={item.get('name')}  api_type={item.get('api_type')}  model={item.get('model')}  priority={item.get('priority')}")
    else:
        print(f"  类型: {type(cur).__name__}  值: {repr(cur)[:120]}")


def _scan_env_files() -> dict[str, int]:
    """扫描可能的 .env 文件，返回 {path: provider_count_in_personification_api_pools}。"""
    env_candidates = [
        "/bot/shizuku/.env.prod", "/bot/shizuku/.env",
        "./.env.prod", "./.env",
        os.path.expanduser("~/.env.prod"), os.path.expanduser("~/.env"),
    ]
    found: dict[str, int] = {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        return found
    for path in env_candidates:
        if not os.path.exists(path) or path in found:
            continue
        try:
            d = dotenv_values(path)
            raw = d.get("personification_api_pools", "")
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
                found[path] = len(parsed) if isinstance(parsed, list) else 0
            except json.JSONDecodeError:
                found[path] = -1  # JSON 解析失败
        except Exception:
            continue
    return found


runtime_candidates = [
    "/bot/shizuku/data/personification/runtime_config.json",
    "./data/personification/runtime_config.json",
    os.path.expanduser("~/data/personification/runtime_config.json"),
]
env_json_candidates = [
    "/bot/shizuku/data/personification/env.json",
    "./data/personification/env.json",
    os.path.expanduser("~/data/personification/env.json"),
]

print("=== 1. .env 文件扫描 ===")
env_results = _scan_env_files()
if not env_results:
    print("  未找到任何包含 personification_api_pools 的 .env 文件")
else:
    for path, count in env_results.items():
        flag = "❌ JSON 解析失败" if count < 0 else f"providers={count}"
        print(f"  {path}: {flag}")

print("\n=== 2. runtime_config.json ===")
rt = _find_first_existing(runtime_candidates)
if rt is None:
    print("  未找到 runtime_config.json")
else:
    with open(rt, "r", encoding="utf-8") as f:
        rt_data = json.load(f)
    print(f"  路径: {rt}")
    _print_api_pools("managed_globals.api_pools", rt_data, ["managed_globals", "api_pools"])

print("\n=== 3. env.json（关键检查）===")
ej = _find_first_existing(env_json_candidates)
if ej is None:
    print("  未找到 env.json（这是正常的，说明没有运行时持久化）")
else:
    with open(ej, "r", encoding="utf-8") as f:
        ej_data = json.load(f)
    print(f"  路径: {ej}")
    _print_api_pools("personification_api_pools", ej_data, ["personification_api_pools"])

print("\n=== 4. 诊断结论 ===")
env_pool_count = max((c for c in env_results.values() if c > 0), default=0)
rt_has = isinstance(rt and json.load(open(rt, encoding="utf-8")).get("managed_globals", {}).get("api_pools"), list) if rt else False
ej_has = isinstance(ej and json.load(open(ej, encoding="utf-8")).get("personification_api_pools"), list) if ej else False
if env_pool_count and (rt_has or ej_has):
    print(f"  ⚠️ .env 已配置 {env_pool_count} 个 provider，但 runtime_config / env.json 中仍残留 api_pools 持久化值")
    print("  → 启动加载顺序可能让残留旧值覆盖 .env 新值")
    print("  → 修复：删除两份 JSON 中的 api_pools 字段，或直接删整个 env.json，重启")
elif env_pool_count and not rt_has and not ej_has:
    print(f"  ✓ .env 配置 {env_pool_count} 个 provider，没有残留持久化值")
else:
    print("  请人工核对上方扫描结果。")
