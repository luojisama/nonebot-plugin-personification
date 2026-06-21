---
name: weather
description: 查询指定城市当前天气或未来 1-16 天天气预报，并返回简洁天气文本
entrypoint: main.py
parameters:
  type: object
  properties:
    city:
      type: string
      description: 城市名，支持常见别名（如 魔都、帝都）
    days:
      type: integer
      description: 预报天数，当前天气用 1；未来几天/这周/半个月用对应天数，最大 16
      default: 1
      minimum: 1
      maximum: 16
  required:
    - city
---

用于回答“某地天气怎么样”的问题。

- 输入 city 后返回当前天气摘要；输入 days>1 时返回未来多日天气趋势
- 内置别名映射并兼容配置扩展
- 如果用户没有明确城市，应先根据当前用户档案、记忆或上下文确认可靠地点；没有可靠地点时不要猜城市，先自然追问
- 问“未来几天”“这周”“半个月”这类范围时传 days，最大 16
- 最终回复要像群友自然接话，不要暴露工具/API/搜索过程，不要使用 markdown 列表
