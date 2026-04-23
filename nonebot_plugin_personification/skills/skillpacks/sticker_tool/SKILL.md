---
name: sticker_tool
description: 表情包选择与图像上下文读取能力
entrypoint: main.py
parameters:
  type: object
  properties:
    action:
      type: string
      description: select 或 context
    mood:
      type: string
      description: 情绪关键词，select 时使用
    context:
      type: string
      description: 对话摘要，select 时使用
    proactive:
      type: boolean
      description: 是否主动发送
    sticker_dir:
      type: string
      description: 贴图目录，默认 data/personification/sticker
    user_text:
      type: string
      description: context 动作时可传入补充文本
  required:
    - action
---

用于选择最匹配表情包或读取当前图像上下文状态。
