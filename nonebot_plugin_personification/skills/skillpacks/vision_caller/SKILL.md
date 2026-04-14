---
name: vision_caller
description: 构建视觉调用器并可执行单次图片描述
entrypoint: main.py
parameters:
  type: object
  properties:
    action:
      type: string
      description: build 或 describe
    api_type:
      type: string
      description: 模型类型，如 openai、gemini_official、anthropic
    api_key:
      type: string
      description: 视觉模型 API Key
    base_url:
      type: string
      description: API 基础地址
    model:
      type: string
      description: 模型名称
    prompt:
      type: string
      description: describe 时传入的提示词
    image_url:
      type: string
      description: describe 时传入的图片 URL 或 data URL
  required: []
---

用于视觉能力自检与单次图片识别验证。
