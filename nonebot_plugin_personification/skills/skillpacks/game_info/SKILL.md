# game_info

为拟人插件提供游戏信息聚合工具，覆盖热门网游、3A 大作与小众独立游戏。

公开工具：

- `game_info(game, aspect, query?)`

`aspect` 取值：

- `update`：更新内容 / 补丁公告 / 新版本。三路并行：
  - **Steam 官方公告**（store 搜索解析 appid → ISteamNews，免 key），覆盖 Steam 上架的 3A/独立/EA/PS PC 移植等；
  - **头部游戏精选官方源**（`_OFFICIAL_GAME_SOURCES`，预设 10 网游 + 10 单机，不限平台）：
    网游 = 原神/星铁/绝区零/鸣潮/王者荣耀/三角洲行动/英雄联盟/无畏契约/永劫无间/守望先锋；
    单机 = 黑神话悟空/怪物猎人荒野/艾尔登法环/塞尔达·王国之泪/FF7重生/赛博朋克2077/博德之门3/刺客信条影/战神诸神黄昏/双影奇境。
    抓取官方公告页正文；官网为 SPA 时正文可能不全，但**始终保留官方链接作为权威来源**；
  - **社区定向搜索**兜底（国服网游 / 未收录游戏）。
- `guide`：攻略 / 流程 / 通关。社区定向搜索（游民星空、3DM、米游社、B站、贴吧、reddit、gamefaqs 等）。
- `story`：剧情 / 设定 / 世界观。优先 `wiki_lookup`（Fandom/萌娘/维基），并补一条社区搜索覆盖小众游戏。
- `tips`：技巧 / 进阶 / 注意事项。社区定向搜索。

数据源与实现复用：

- Steam Store 搜索 + ISteamNews（免 key）
- 头部游戏精选官方源映射 `_OFFICIAL_GAME_SOURCES`（腾讯/网易/米哈游/拳头/暴雪/EA），用 `core/web_fetch` 抓取正文 + 保留官方链接
- `resource_collector` 的免 key 多搜索引擎抓取（DuckDuckGo + Bing），社区站点已含 TapTap/NGA/A9VG/PSNINE 等手游与主机社区
- `wiki_search` 的 `wiki_lookup`
- 若 `runtime.tool_caller` 可用，会把多源结果压缩成一段可读摘要，并附参考来源；否则直接返回结构化结果。

使用原则：

- 「某款游戏 + 某个方面（更新/攻略/剧情/技巧）」的查询统一走 `game_info`
- 纯角色/世界观/设定的百科式查询也可直接 `wiki_lookup`
- 宽泛的资源合集 / 链接收集走 `collect_resources`
- 查不到时如实承认，不要编造攻略或剧情

相关配置（`config.py`）：

- `personification_game_info_enabled`：开关（默认开）
- `personification_game_info_timeout`：Steam API / 搜索超时（默认 15s）
- `personification_game_info_community_sites`：社区站点白名单覆盖（留空用内置默认）
