---
name: tool_caller
description: 模型路由参数规范化与 data URL 解析辅助
entrypoint: main.py
parameters:
  type: object
  properties:
    api_type:
      type: string
      description: 原始 API 类型
    base_url:
      type: string
      description: 原始 API 地址
    data_url:
      type: string
      description: 可选 data URL，用于解析 MIME 类型
  required: []
---

用于调试 tool_caller 参数归一化结果。
