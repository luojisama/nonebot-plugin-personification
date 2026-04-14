---
name: user_persona
description: 根据聊天记录生成画像分析提示词
entrypoint: main.py
parameters:
  type: object
  properties:
    messages:
      type: array
      items:
        type: string
      description: 用户最近聊天记录
    previous_persona:
      type: string
      description: 可选，已有画像文本
  required:
    - messages
---

用于输出画像分析 prompt，供上层模型调用使用。
