---
name: memory_palace
enabled: true
entrypoint: scripts/main.py
description: 从双记忆系统中召回近期情节、人物印象、群聊上下文和长期主题
parameters:
  type: object
  properties:
    query:
      type: string
    scope:
      type: string
  required:
    - query
---
