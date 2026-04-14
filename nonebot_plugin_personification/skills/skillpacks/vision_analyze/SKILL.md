---
name: vision_analyze
enabled: true
entrypoint: scripts/main.py
description: 分析当前图片并输出结构化视觉线索
parameters:
  type: object
  properties:
    query:
      type: string
    images:
      type: array
      items:
        type: string
  required:
    - query
---
