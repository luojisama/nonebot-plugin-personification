---
name: vision_analyze
enabled: true
entrypoint: scripts/main.py
description: 分析当前图片或视频并输出结构化视觉线索；视频支持全模态原生理解或分镜与音频转写降级
parameters:
  type: object
  properties:
    query:
      type: string
    images:
      type: array
      items:
        type: string
    videos:
      type: array
      items:
        type: string
  required:
    - query
---
