---
name: acg_resolver
enabled: true
entrypoint: scripts/main.py
description: 对高歧义 ACG 实体做作品/角色/术语消解
parameters:
  type: object
  properties:
    query:
      type: string
    image_context:
      type: boolean
  required:
    - query
---
