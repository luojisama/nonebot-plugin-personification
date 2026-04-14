---
name: web_search
description: 联网搜索实时信息并返回整理后的结果
entrypoint: main.py
parameters:
  type: object
  properties:
    query:
      type: string
      description: 搜索关键词，简洁明确
  required:
    - query
---

用于用户需要最新事实或实时信息时的联网检索。

- 传入 query 执行联网搜索
- 自动应用插件搜索前缀配置
- 返回聚合后的可读文本
