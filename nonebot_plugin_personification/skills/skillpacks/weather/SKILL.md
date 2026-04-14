---
name: weather
description: 查询指定城市天气并返回简洁天气文本
entrypoint: main.py
parameters:
  type: object
  properties:
    city:
      type: string
      description: 城市名，支持常见别名（如 魔都、帝都）
  required:
    - city
---

用于回答“某地天气怎么样”的问题。

- 输入 city 后返回当前天气摘要
- 内置别名映射并兼容配置扩展
