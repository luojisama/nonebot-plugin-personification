# 原生社交平台查梗 MCP

`builtin_social_platform_research` 是项目内置、只读、默认关闭的 MCP 服务，用于在管理员自己的登录态下检索 B站、抖音、贴吧和小黑盒，补足普通网页搜索无法稳定覆盖的游戏黑话、梗、外号和版本语境。平台材料始终标记为 `untrusted_data_only`；MCP 只负责取得、标准化和过滤材料，词义提取、sense 聚类、多源确认和词典写入由宿主侧 Agent 流水线完成。

## 开启与登录

1. 启动 FastAPI Driver，使用管理员账号进入 WebUI。
2. 打开“MCP → 原生 MCP”，开启服务总开关。
3. 逐个平台开启 B站、抖音、贴吧或小黑盒。
4. 优先点击二维码按钮。B站调用官方固定二维码生成接口，在服务端本机编码 PNG，并用同一 persistent context 的请求上下文轮询，因此不打开也不依赖可见窗口；其它平台仍从各自官方页面截取真实二维码。管理会话保持 15 分钟，官方二维码自身过期时会在同一会话中换新。
5. 若小黑盒二维码仍无法扫描，或抖音出现机器人验证，点击“在普通浏览器中登录”。MCP 会关闭自动化窗口，使用同一隔离 profile 直接启动不受 Playwright 控制的系统 Chrome/Edge，并给人工登录保留 30 分钟。B站协议二维码异常时也保留此人工兜底。
6. 在普通浏览器窗口中完成扫码、短信验证码、滑块、机器人验证或官方 App 确认；成功后完全关闭该隔离窗口。MCP 只在窗口关闭后检测登录 Cookie，不会在验证期间抢占 profile，也不会操作或绕过验证。
7. 平台状态变为 `ready` 后，再逐项授权 Agent 工具。
8. 使用检索预览读取一个真实词，确认封面、标题、正文/文案、评论/回复和能力矩阵符合预期。

停止或重载服务不会清除登录态。注销要求 WebUI 精确确认，只删除所选平台的独立 profile，不影响其它平台。服务不实现验证码或风控绕过，也不承诺平台页面改版后无需更新选择器。重新获取二维码会取消同一管理员、同一平台的旧登录会话，旧二维码不再用于状态判断。

当前登录适配基线（2026-07-26）：B站官方页使用 `/x/passport-login/web/qrcode/generate` 生成事务、`/x/passport-login/web/qrcode/poll` 轮询；当前官方前端把 `86101` 解释为未扫码、`86090` 解释为扫码后待确认、`86038` 解释为过期、`0` 解释为成功。MCP 只调用这两个固定 HTTPS 端点，严格校验二维码 URL 为 `account.bilibili.com/h5/account-h5/auth/scan-web`，不读取或返回成功响应中的 Token/跳转字段；未知状态失败关闭。事务键只保存在 MCP 内存，不进入 JSON、日志、Trace、审计、诊断或知识库。小黑盒继续让官方页面生成带 `hkey/nonce` 的请求，不复制签名算法；官方 `website-login__qr-canvas` 初次可见时只是“二维码加载中”占位图，MCP 在后台等待其通过黑白比例、对比度和边缘密度校验后才转发真正二维码。抖音使用官方“二维码”图片。二维码内容 revision 让 WebUI 在换新后取到新图，而不是继续显示浏览器缓存中的旧图。WebUI 使用带白色静区的 240px 近邻缩放展示 PNG，并显示后端计算的 `remaining_seconds`。

## 三层可用条件

一次 Agent 调用必须同时满足：

- MCP 服务总开关开启且子进程存活；
- 请求指向的平台至少有一个已开启并处于 `ready`；
- 对应工具已经由管理员授权并注册。

平台被关闭后，搜索与读取不会再访问该平台。一个平台出现 `risk_controlled`、`manual_verification_required`、登录过期或浏览器异常时，其它健康平台仍可返回 `partial=true` 的内容包。

## Agent 工具

| 工具 | 用途 | 写操作 |
|---|---|---|
| `social_content_search` | 按查询词、平台、内容类型和质量模式搜索内容卡片 | 无 |
| `social_content_read` | 按平台内容 ID 或 URL 读取正文、评论、回复、弹幕/字幕 | 无 |
| `research_game_slang` | 针对一个词和当前游戏语境做跨平台两阶段查证 | 无 |

管理面的登录、状态、配置、取消和注销使用私有 JSON-RPC，只允许 WebUI 后端调用，不出现在 `tools/list`。MCP 不公开任意 HTTP、任意浏览器操作、任意 JavaScript、Cookie 导出或平台管理工具。

## 平台能力

| 平台 | 搜索 | 正文/文案 | 评论/回复 | 弹幕 | 备注 |
|---|---:|---:|---:|---:|---|
| B站 | 是 | 是 | 是 | 是 | 可取得时同时读取字幕 |
| 抖音 | 是 | 是 | 是 | 页面提供时 | 登录与风控状态以当前官方页面为准 |
| 贴吧 | 是 | 是 | 楼层与回复 | 否 | 支持全局/吧内页面结果 |
| 小黑盒 | 是 | 是 | 是 | 仅真实视频支持时 | 文章、动态和帖子按实际页面能力声明 |

每个平台使用独立 Playwright persistent context。B站二维码使用 `BrowserContext.request` 与浏览器上下文共用的登录态容器，官方响应写入同一 profile；其它登录窗口优先使用本机正式 Chrome/Edge，读取阶段继续使用同一受限 profile。代码不伪造 User-Agent，也不隐藏自动化标识来规避平台检测。适配器只允许固定官方登录端点，或解析官方页面及页面自身产生的 XHR/Fetch 响应，不维护通用私有签名客户端。

## 来源质量与缓存

默认过滤规则如下：

- 视频：`marketing_score >= 0.75` 排除；仅在 `play_count < 3000` 且 `comment_count < 5` 同时成立时按低热度排除。
- 文章/帖子：明确营销内容排除；默认仅在 `reply_count < 3` 且其它已知互动均为零时按低互动排除。
- `balanced` 会执行默认过滤，`strict` 更保守，`ranking_only` 保留候选并只影响排序。
- 营销信号必须给出原因，例如商业标识、联系方式、外部导流、重复文案、外链或行动号召密度。
- 标准化搜索/内容包默认缓存 6 小时；不缓存完整视频、Cookie 或原始整页 HTML。

评论、回复、弹幕采样量、平台阈值、最大结果数、缓存时间和限流均可在平台卡片单独调整。封面仅通过短期 `cover_ref` 代理读取，代理限制平台域名、HTTPS、重定向、图片 Content-Type 和 5 MiB 大小。

## 多梗提取与自动学习

一份内容包默认最多提取 20 个 claim，可在 1–50 之间配置。同一视频可以同时解释“刘涛”“牢大”“大红”等多个 term；每条 claim 必须引用标题、正文、评论、回复或弹幕中的具体“词语 → 含义”片段。只有出现词语、没有解释关系的共现不算证据。

独立来源按内容簇计数：同一内容下再多评论也只贡献一份；跨平台搬运、相同媒体指纹、明确转载、相同外部来源或高度近似正文会合并。营销过滤、失效引用、风控不完整和低于 `claim_min_confidence` 的材料不参与自动确认。

状态机：

- `observed`：一个独立内容，仅留作候选；
- `understand_only`：默认至少两个独立内容一致，只可理解或被问时解释；
- `verified`：默认至少三个独立内容、覆盖至少两个平台，可在匹配语境中自然使用；
- `disputed`：同一游戏/版本下相反含义也达到实质支持，停止主动使用；
- `stale`：长期没有新鲜证据，停止主动使用但保留历史；
- `rejected`：管理员拒绝；
- `manual_locked`：管理员确认或编辑，自动流水线只能追加证据和提示冲突。

不同游戏、版本或赛季保存为不同 sense，不以简单多数覆盖。自动词义携带 `game_context`、`version_context`、`usage_context` 和 `safe_usage`；没有匹配上下文时不会注入游戏义。

## 配置

| 配置键 | 默认值 | WebUI 约束 | 说明 |
|---|---:|---:|---|
| `personification_meme_reply_probability` | `0.18` | `0–1` | 已决定回复后，是否允许自然带一个低风险 active sense |
| `personification_slang_max_claims` | `20` | `1–50` | 单个内容包最多提取的 claim 数 |
| `personification_auto_understand_min_sources` | `2` | `2–20` | 进入 `understand_only` 的独立内容数 |
| `personification_auto_use_min_sources` | `3` | `2–30` | 进入 `verified` 的独立内容数 |
| `personification_auto_use_min_platforms` | `2` | `2–4` | 进入 `verified` 的平台覆盖数 |
| `personification_claim_min_confidence` | `0.72` | `0–1` | claim 进入自动聚合的最低置信度 |
| `personification_semantic_equivalence_min_confidence` | `0.80` | `0–1` | sense same/compatible/conflict 判断门槛 |
| `personification_reverify_after_days` | `30` | `1–365` | verified 进入复核窗口的天数 |
| `personification_stale_after_days` | `90` | `2–730` | 无新证据后降为 stale 的天数 |

独立来源门槛在运行时还会执行安全归一，最低不得小于 2；`stale_after_days` 必须晚于复核窗口。`personification_probability` 仍为 0.30，玩梗抽样不会额外触发一条本来不会发送的群消息。

## 秘密和导出边界

以下内容不得返回 Agent、普通 WebUI JSON、日志、Trace、审计详情、诊断包、知识库或 Data Transfer：

- Cookie、Token、验证码、手机号、设备标识和平台请求签名；
- 二维码登录 session 与管理员/device owner 绑定信息；
- Playwright browser profile 路径及 profile 内文件；
- 脱敏作者指纹；
- 短期完整评论缓存、原始整页 HTML 和完整视频。

Data Transfer v3 只导出逻辑词典根、sense、短证据和学习事件。证据 URL 会移除 query/fragment，`author_fingerprint` 在包 schema 中不存在，导入后固定为空；旧 v1/v2 包继续可读，v2 包不能声明 v3 的 `meme_dictionary` dataset。

## 状态排查

| 状态 | 含义 | 处理 |
|---|---|---|
| `disabled` | 平台开关关闭 | 开启平台后再检查登录 |
| `login_required` | 未登录或登录过期 | 重新扫码/官方确认 |
| `ready` | 可进行只读请求 | 再检查工具授权和查询输入 |
| `manual_verification_required` | 官方页面要求人工验证 | 在官方页面完成，不要自动重试或绕过 |
| `risk_controlled` | 平台风控 | 暂停该平台，等待解除；使用其它平台 partial 结果 |
| `qr_expired` | 官方二维码已经失效 | 等待同一会话自动刷新；连续失败时改用普通浏览器登录 |
| `error` + `official_window_closed` | 页面二维码链路在登录完成前被关闭 | 重新获取二维码，并保持该平台官方窗口打开到 `success`；不适用于 B站 `protocol_qr` |
| `error` + `bilibili_qr_*` | B站固定官方二维码事务返回异常、未知状态或本地编码不可用 | 重载后重新获取；不要改用非官方接口或导出事务键 |
| `unavailable` | 浏览器、页面或适配器异常 | 运行健康诊断并检查 Chromium/Playwright 与页面改版 |

登录态、平台开关和 Agent 工具授权是三个独立状态。卡片显示 `success` 只表示登录 Cookie 已保存到该平台的隔离 profile，不会自动把平台从 `disabled` 改成 `ready`；登录成功后的管理会话倒计时也不会删除 profile 登录态。随后仍需点击“开启平台”，并在服务卡片中授权所需工具。只有三层均满足时，顶部 `EFFECTIVE` 才会大于零。

平台配置 JSON 只允许过滤、采样、缓存与超时字段，`enabled` 只能作为独立控制字段传递。native MCP 状态、WebUI API 和浏览器提交都会剥离嵌套的 `config.enabled`；否则严格配置校验会拒绝开启请求。若升级前曾看到“登录成功”但仍为 `revision=0 / disabled`，请部署本修复并重载原生 MCP 后重新点击“开启平台”，不需要注销或重新扫码。

`manual_verification_required` 还会携带不含页面正文的安全分类：

- `verification_kind=robot_verification`：抖音等平台显示机器人/滑块验证，只能在官方窗口人工完成；状态轮询只读取页面状态和登录 Cookie，不会点击验证控件。
- `verification_kind=official_browser_login`：已自动或人工切换到普通系统浏览器。完成登录后关闭该窗口，MCP 才会重新打开同一隔离 profile 做只读 Cookie 检测。
- `verification_kind=manual_login_incomplete`：普通浏览器已经关闭，但没有检测到有效登录 Cookie；重新打开普通浏览器继续，不会删除 profile。
- `verification_kind=device_confirmation`：二维码已扫描，需要在官方 App 中确认；B站 `protocol_qr` 不要求任何浏览器窗口保持打开。
- `verification_kind=qr_generation_blocked`：官方扫码面板已经打开，但二维码区域只有 Logo/占位图，没有真实二维码；在官方窗口完成人工验证或使用平台提供的验证码登录，MCP 不会把占位图转发到 WebUI。
- `verification_kind=official_page`：官方页面没有提供可以安全转发到 WebUI 的二维码，需要直接在官方窗口完成登录。

WebUI 的二维码 URL 带 `qr_revision`，响应始终为 `Cache-Control: no-store, private`。页面延迟出码、二维码自动更新或扫码后切换确认状态时，不需要重启 MCP。

抖音未登录页面也可能下发通用 `passport_csrf_token`；它不是登录成功证据。适配器只以真实会话 Cookie 判断抖音登录完成，避免机器人验证或手机确认尚未结束时误报 `success` 并关闭官方窗口。

## 真实验收清单

四个平台的自动测试只能验证协议、隔离、过滤、解析和失败边界；首版完成仍要求管理员在真实账号上逐个平台执行：登录、重启后保持登录、搜索同一真实黑话、读取封面/正文/评论/回复、检查支持平台弹幕、从一份内容提取多个 claim、关闭平台后确认零访问、注销后确认 profile 删除，并确认全过程没有任何平台写操作。

2026-07-26 使用隔离临时 profile 对 B站、抖音和小黑盒官方页面做了未登录真实探针，三者均返回 `waiting_scan`、`qr_available=true`、`qr_revision=1`，随后已关闭窗口并删除临时 profile。使用比新版 WebUI 更高频的 1.8 秒连续轮询时，B站和小黑盒 25 秒、抖音 42 秒内均保持 `waiting_scan`，服务端剩余时间按秒正常递减，没有出现三秒过期。新版 WebUI 改为 3 秒单飞轮询，前一次状态请求没有结束时不会叠加下一次。普通浏览器路径另做了 8 秒真实进程探针：从 `1799` 秒递减到 `1791` 秒，窗口持续存活且未调用 Playwright。以上只证明会话寿命、二维码状态和普通浏览器交接有效，不等于真实账号登录、扫码后验证或内容读取验收。

同日针对无法扫码问题继续核对官方实时实现：B站官方登录页实际调用固定 generate/poll 端点，官方前端 bundle 仍按 `86101/86090/86038/0` 驱动未扫码、待确认、过期和成功。改为 `protocol_qr` 后，真实未登录探针从 `898` 秒运行到 `895` 秒，始终 `waiting_scan`、`qr_revision=1`、`official_window_open=false`。小黑盒视觉探针确认 canvas 可见初期确实是“二维码加载中”占位图，约数秒后才绘制黑白二维码；启用像素结构门和 `headless_page_qr` 后，真实探针从 `896` 秒运行到 `893` 秒，返回通过结构校验的 PNG，期间不需要可见窗口。临时 profile 和视觉探针图片均已删除。该证据验证了未扫码阶段和占位图修复，扫码到 `success` 仍需管理员用真实账号完成。

本轮最终验证：完整 pytest 使用 Windows 短 `--basetemp=.pf` 得到 `1946 passed, 3 warnings`；长 basetemp 下出现的 3 个 Remote Skill snapshot 失败按项目已知路径限制改用 `.pt` 定向复跑为 `15 passed`。聊天模拟器 `9/9`、personification semantic scan、Python compileall/py_compile、10 个 WebUI JavaScript `node --check` 和 `git diff --check` 均通过。真实 stdio MCP 私有 RPC smoke 依次执行 `auth/start → auth/qrcode → auth/status`：B站返回 `protocol_qr`，小黑盒返回 `headless_page_qr`，两者 3 秒后仍 `waiting_scan`、`official_window_open=false`、`qr_revision=1`，且 QR PNG 可取；临时 profile 在子进程关闭后删除。
