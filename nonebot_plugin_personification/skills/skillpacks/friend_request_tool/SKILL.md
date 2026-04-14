---
name: friend_request_tool
description: 检查好友申请是否满足频控门禁
entrypoint: main.py
parameters:
  type: object
  properties:
    user_id:
      type: string
      description: 目标用户ID
    daily_limit:
      type: integer
      description: 每日申请上限，默认 2
  required:
    - user_id
---

用于执行好友申请前置检查：
- 是否超过当日申请上限
- 是否命中同用户冷却时间
