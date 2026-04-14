---
name: resource_collector
description: 先由 LLM 理解资源需求并生成搜索计划，再执行搜索并整合结果
entrypoint: scripts/main.py
---

# resource_collector

用于搜集教程、图片、视频、文档、社区帖子等资源。

## 工具

### `confirm_resource_request`
由 LLM 判断用户到底想找什么资源；如果需求不完整，返回澄清问题。

### `collect_resources`
资源搜集主工具。内部流程是：

1. LLM 理解用户意图
2. LLM 生成搜索计划
3. 按计划执行搜索
4. LLM 基于真实结果整合输出

### `search_web` / `search_github_repos` / `search_official_site` / `search_images`
原始结构化检索工具。适合模型已经明确知道自己要查什么时直接调用。

## 原则

- 不要靠固定关键词先决定资源类型或搜索方式
- 优先让 `collect_resources` 处理攻略、教程、资料页、链接合集等资源请求
- 只允许基于实际搜索结果整理答案，不允许编造链接
- 如果结果不足，应明确说明没找到，并给出更好的搜索方向
