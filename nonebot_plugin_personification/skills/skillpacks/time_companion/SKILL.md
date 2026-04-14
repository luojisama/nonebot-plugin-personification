---
name: time_companion
description: 提供更生活化的时间问候与时间片建议
entrypoint: main.py
parameters:
  type: object
  properties:
    timezone:
      type: string
      description: 时区名称，默认 Asia/Shanghai
    mood:
      type: string
      description: 语气风格，可选 gentle、energetic、calm
  required: []
---

当用户询问现在几点、该做什么、是否适合休息时使用本 skill。

输出应简洁、自然，避免工具腔，突出陪伴感。
