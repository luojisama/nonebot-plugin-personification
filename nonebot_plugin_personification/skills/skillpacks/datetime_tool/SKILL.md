---
name: datetime_tool
description: 提供当前时间、时段与节气信息
entrypoint: main.py
parameters:
  type: object
  properties:
    timezone:
      type: string
      description: 时区名称，默认 Asia/Shanghai
  required:
    - timezone
---

用于时间问答场景，返回结构化时间信息：
- 当前日期与时刻
- 星期信息
- 时段（早晨/下午/晚上等）
- 节气
