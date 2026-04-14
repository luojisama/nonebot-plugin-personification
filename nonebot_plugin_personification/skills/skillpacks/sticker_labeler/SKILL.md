---
name: sticker_labeler
description: 表情包打标辅助工具，支持图片转 data URL 与结果归一化
entrypoint: main.py
parameters:
  type: object
  properties:
    action:
      type: string
      description: to_data_url 或 normalize
    file_path:
      type: string
      description: 图片文件路径，to_data_url 时必填
    raw_json:
      type: string
      description: 模型原始 JSON 文本，normalize 时必填
    model_name:
      type: string
      description: 可选，打标模型名称
  required:
    - action
---

用于贴图标注链路中的预处理/后处理，不直接执行大批量扫描。
