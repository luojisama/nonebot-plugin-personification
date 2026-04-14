---
name: group_info_tool
description: 输出白名单群信息结构
entrypoint: main.py
parameters:
  type: object
  properties:
    group_ids:
      type: array
      items:
        type: string
      description: 白名单群号列表
    group_name_map:
      type: object
      description: 可选的群号到群名映射
  required: []
---

用于将群号和群名组织为标准 JSON 列表。
