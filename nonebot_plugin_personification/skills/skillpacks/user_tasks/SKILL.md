---
name: user_tasks
description: 管理用户定时任务，支持创建与取消
entrypoint: main.py
parameters:
  type: object
  properties:
    operation:
      type: string
      description: 操作类型，create 或 cancel
    user_id:
      type: string
      description: 用户 ID
    task_id:
      type: string
      description: 要取消的任务ID，仅 cancel 使用
    description:
      type: string
      description: 任务描述，仅 create 使用
    cron:
      type: string
      description: cron 表达式，仅 create 使用
    action:
      type: string
      description: 任务动作名，仅 create 使用
    params:
      type: object
      description: 任务动作参数，仅 create 使用
  required:
    - operation
    - user_id
---

用于用户个人任务管理，不依赖调度器即可记录任务状态。

- create：创建任务并持久化
- cancel：按 task_id 取消任务
- 还提供 run_create / run_cancel 两个显式入口
