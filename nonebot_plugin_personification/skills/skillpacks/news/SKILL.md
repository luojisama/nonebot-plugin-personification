---
name: news
description: 聚合新闻/热搜/段子/历史/汇率等信息并返回可读摘要
entrypoint: main.py
parameters:
  type: object
  properties:
    topic:
      type: string
      description: 能力类型，支持 daily/ai_news/tech_news/trending/joke/history/epic/gold/baike/exchange
    platform:
      type: string
      description: trending 模式的平台，支持 微博/知乎/抖音/B站/百度/头条/小红书
    source:
      type: string
      description: tech_news 模式的来源，支持 IT之家/Hacker News
    keyword:
      type: string
      description: baike 模式的词条关键词
    base_currency:
      type: string
      description: exchange 模式的基准货币，如 CNY
    quote_currency:
      type: string
      description: exchange 模式的目标货币，如 USD
  required:
    - topic
---

用于把资讯类请求整理成结构化摘要文本，避免模型自行拼接不稳定来源。

- 日常新闻：topic=daily
- AI 资讯：topic=ai_news；当天无内容时自动返回最近一期
- 科技资讯：topic=tech_news + source=IT之家/Hacker News
- 平台热搜：topic=trending + platform=微博/知乎/抖音/B站/百度/头条/小红书
- 段子：topic=joke
- 历史上的今天：topic=history
- Epic 免费游戏：topic=epic
- 黄金价格：topic=gold
- 百科词条：topic=baike + keyword=关键词
- 汇率：topic=exchange + base_currency/quote_currency
