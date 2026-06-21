#!/usr/bin/env python3
"""诊断 dotenv 读取 personification_api_pools 的结果"""
import json
import sys

try:
    from dotenv import dotenv_values
except ImportError:
    print("python-dotenv 未安装，尝试手动解析 .env.prod")
    dotenv_values = None

import os

env_path = ".env.prod"
if not os.path.exists(env_path):
    # 尝试从常见路径找
    for candidate in ["/bot/shizuku/.env.prod", "/opt/bot/.env.prod", os.path.expanduser("~/.env.prod")]:
        if os.path.exists(candidate):
            env_path = candidate
            break

print(f"读取文件: {env_path}")
print(f"文件存在: {os.path.exists(env_path)}")

val = None

if dotenv_values is not None:
    d = dotenv_values(env_path)
    val = d.get("personification_api_pools", "")
    print(f"\n=== dotenv_values 读取 ===")
    print(f"类型: {type(val).__name__}  长度: {len(val) if val else 0}")
    print("--- 完整内容 ---")
    print(repr(val))
else:
    # 手动读取
    print("\n=== 手动读取原始内容 ===")
    with open(env_path, "r", encoding="utf-8") as f:
        raw = f.read()
    # 找到 personification_api_pools 行
    lines = raw.splitlines()
    capturing = False
    captured = []
    for line in lines:
        if line.startswith("personification_api_pools"):
            capturing = True
            # 取等号后的值
            _, _, rest = line.partition("=")
            captured.append(rest)
        elif capturing:
            if line.startswith(" ") or line.startswith("\t") or (captured and not line.startswith("#") and "=" not in line):
                captured.append(line)
            else:
                break
    val = "\n".join(captured).strip().strip("'\"")
    print(f"原始提取内容:\n{val}")

print()
if val:
    # 尝试 JSON 解析
    try:
        parsed = json.loads(val)
        print(f"JSON 解析成功，元素数: {len(parsed)}")
        for item in parsed:
            if isinstance(item, dict):
                print(f"  - name={item.get('name')}  priority={item.get('priority')}")
    except Exception as e:
        print(f"JSON 解析失败: {e}")
        # 尝试去掉单引号包裹
        stripped = val.strip("'")
        try:
            parsed = json.loads(stripped)
            print(f"去掉单引号后 JSON 解析成功，元素数: {len(parsed)}")
            for item in parsed:
                if isinstance(item, dict):
                    print(f"  - name={item.get('name')}  priority={item.get('priority')}")
        except Exception as e2:
            print(f"去掉单引号后仍失败: {e2}")
else:
    print("警告: 未读取到 personification_api_pools 值")
