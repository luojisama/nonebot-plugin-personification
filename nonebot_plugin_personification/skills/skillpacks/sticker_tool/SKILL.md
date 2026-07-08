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
    media_type:
      type: string
      description: static=静态图，gif=只选 GIF 动图，any=静态/GIF 都可
    sticker_dir:
      type: string
      description: 贴图目录，默认 data/personification/sticker
    user_text:
      type: string
      description: context 动作时可传入补充文本
  required:
    - action
---

用于选择最匹配表情包或读取当前图像上下文状态；select 动作支持按 media_type 获取 GIF 动态表情。
