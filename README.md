<div align="center">

# 🌌 AstrBot 绯色万象

<i>支持多图片源、动态标签、故障切换与自动撤回的 AstrBot 图片插件。</i>

![Version](https://img.shields.io/badge/version-v0.3.3-blue)

</div>

---

## 📖 简介

astrbot_plugin_crimson_cosmos 会在指定群聊或私聊中监听关键词，从 Lolicon、Wallhaven 或自定义 API 获取图片。插件支持数量与标签解析、多来源重试、OneBot 合并转发，以及发送后自动撤回。
同时支持通过 `/av` 查询 Jable 的热门、新片、主题和女优影片信息，以及搜索 MissAV 作品和获取磁力链接。
支持通过 `/jm` 搜索、查看和下载 JM 本子，下载结果使用普通 ZIP 发送。

---

## ✨ 功能特性

- 🎨 **多图片源** - 支持 Lolicon、Wallhaven 和自定义 JSON API。
- 🏷️ **动态标签** - 从消息中提取标签，并支持 Lolicon 标签别名。
- 🔢 **多图请求** - 支持中文或阿拉伯数字，单次最多 5 张。
- ⚡ **并发获取** - 多图请求、来源拉取、代理下载与过审处理均并行执行，降低延迟。
- 🔄 **故障切换** - 来源请求失败后自动重试并尝试备用来源。
- 💬 **图片发送** - 支持逐张发送、单图聊天记录和 OneBot 合并转发。
- ⏱️ **自动撤回** - 撤回延迟可配置，任务可在插件重载后恢复。
- 🛡️ **会话限制** - 可分别控制私聊和允许使用的群聊。
- 📚 **JM 本子** - 支持搜索、详情、日/周/月榜和普通 ZIP 下载。
- 🔎 **MissAV 搜索** - 支持按番号、女优或标题关键词查找作品。
- 🧲 **MissAV 磁力** - 支持从作品详情页返回最多 5 条磁力链接，不自动下载。

---

## 📦 安装

将插件放入以下目录：

~~~text
AstrBot/data/plugins/astrbot_plugin_crimson_cosmos
~~~

安装依赖：

~~~bash
pip install -r requirements.txt
~~~

随后在 AstrBot WebUI 中重载插件或重启 AstrBot。

---

## 🚀 使用方法

先在插件配置中开启私聊，或开启群聊并填写允许的群号，然后配置触发关键词和图片来源。

### 💡 示例

> **输入：** 色图
>
> **行为：** 获取并发送 1 张图片。

> **输入：** 来三份白丝、猫耳色图
>
> **行为：** 使用动态标签获取 3 张图片；超过 5 张时拒绝请求。

Lolicon 标签内置常见中文同义词（白丝、黑丝、猫耳、泳装、萝莉、女仆等），无需手动配置即可自动转换；标签会展开为「同义 OR」请求，并在无结果时自动回退为无标签请求，避免拿不到图。

如需覆盖内置转换或添加自定义标签，再配置别名，格式为每行 `别名=目标标签`：

~~~text
白丝=white_pantyhose
猫耳=cat_ears
DeepSeek=deepseek
~~~

配置别名后，可直接输入 `DeepSeek涩图`，插件会自动提取并转换标签；自定义别名优先于内置同义词。

Jable 查询示例：

~~~text
/helpav
/av 热门 本月 1
/av 热门 本月 1-10
/av 新片 1
/av 主题 黑丝 1
/av 主题 黑丝 最高收藏 1
/av 女优 河北彩花 1
/av 女优 河北彩花 最近更新 1

/av 搜索 SSIS-001 1
/av 磁力 SSIS-001
/av 磁力 https://missav.ws/dm44/cn/ssis-001
~~~

JM 查询与下载示例：

~~~text
/jm 搜索 全彩 1
/jm 详情 123456
/jm 热门 周 1
/jm 下载 123456
~~~

排名支持 1–30，也可使用 `1-10` 这样的连续范围，每次最多获取 10 部。多部影片会合并成聊天记录发送。热门时间范围支持 `今日`、`本周`、`本月`、`全部`。
主题和女优支持 `近期最佳`、`最近更新`、`最多观看`、`最高收藏`；省略时使用网站默认排序。
MissAV 搜索排名支持 1–30；磁力命令默认返回详情页前 5 条磁力链接，并且只接受 `missav.ws` 详情链接。

---

## ⚙️ 配置项说明

### 会话权限与触发规则

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| enable_group | 启用群聊回复 | false |
| allowed_group_ids | 启用群聊回复的群号列表；留空时不回复群聊 | [] |
| enable_private | 启用私聊回复 | false |
| allowed_private_user_ids | 允许触发私聊回复的 QQ 号；留空允许全部 | [] |
| keywords | 触发关键词列表 | ["色图"] |
| keyword_match_mode | exact、prefix 或 contains | exact |
| cooldown_seconds | 按用户和会话限制关键词获取的冷却时间，0 表示关闭 | 0 |
| block_other_handlers | 命中后阻止其他插件和默认大模型同时回复 | true |

### 发送、提示与来源

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| multi_image_send_mode | direct 逐张发送或 forward 合并转发 | direct |
| single_image_forward | 单张图片也通过 OneBot 合并转发聊天记录发送 | false |
| auto_recall | 自动撤回 OneBot 图片消息 | false |
| recall_delay_seconds | 自动撤回延迟，单位秒 | 60 |
| fetching_message | 开始获取提示，留空关闭 | 正在获取喵~ |
| group_disabled_message | 群聊回复关闭且关键词命中时发送，留空关闭 | 本喵暂时不提供此服务喵~ |
| cooldown_message | 冷却期间提示，留空关闭 | 冷却中呢喵~ |
| failure_message | 最终失败提示，留空关闭 | 涩图获取失败了喵，请稍后再试~ |
| image_source | 默认图片来源 | custom |
| image_source_order | 图片来源故障切换顺序 | [] |
| request_retry_count | 每个来源的重试次数 | 3 |

### 过审处理（反拦截）

发送前下载原图并加扰动重新编码，改变图片指纹并干扰内容识别，用于降低图片被平台服务器层拦截的概率。需安装 Pillow（`pip install -r requirements.txt`）。

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| bypass_mode | off 关闭；transform 扰动后按图片发送；file 扰动后按文件发送；transform_file 扰动并按文件发送 | transform |
| bypass_noise | 随机像素噪点强度，破坏图片指纹 | 8 |
| bypass_rotate | 随机旋转角度上限（度），破坏对齐类检测 | 1.0 |
| bypass_flip | 以 50% 概率水平镜像 | true |
| bypass_resize_ratio | 随机微缩放比例，破坏精确哈希（1.00 关闭） | 0.98 |
| bypass_jpeg_quality | JPEG 重编码质量，引入压缩噪声 | 90 |
| bypass_hue_shift | 色相偏移（度），偏移肤色检测（0 关闭） | 0 |
| bypass_brightness | 亮度抖动（1.00 关闭） | 1.0 |

处理失败时会自动回退发送原图，不会导致图片丢失。

### Lolicon

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| lolicon_r18_mode | sfw、r18 或 mix | r18 |
| lolicon_exclude_ai | 排除 AI 图片 | true |
| lolicon_aspect_ratio | 不限、横图、竖图或方图 | 空 |
| lolicon_image_size | 请求图片尺寸：original 原图、regular 常规尺寸、small 小尺寸、thumb 缩略图、mini 极小缩略图 | small |
| lolicon_proxy | 图片反代地址 | 空 |
| lolicon_proxy_order | 图片代理尝试顺序，失败自动切换 | i.loli.best、pixiv.cat、i.pixiv.nl、i.pixiv.re |
| lolicon_proxy_timeout_seconds | 单个图片代理超时秒数 | 8 |
| lolicon_tag_aliases | 自定义标签别名（覆盖内置同义词），留空使用内置常见标签转换 | 空 |
| show_pixiv_pid | 回显 Pixiv PID | false |

### 自定义 API 与 Wallhaven

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| custom_api_url | 自定义 JSON API 地址 | 空 |
| custom_api_image_url_path | 图片 URL 的点号路径 | url |
| custom_api_tag_parameter | 动态标签参数名 | tag |
| wallhaven_api_key | Wallhaven 成人内容 API Key | 空 |
| wallhaven_categories | 图片分类，可选通用、动漫、人物 | ["动漫"] |
| wallhaven_purity | 内容分级，可选全年龄、擦边、成人 | ["成人"] |
| wallhaven_sorting | 排序方式：最新、热门或榜单 | 最新 |
| wallhaven_tags | 固定搜索标签 | [] |

### Jable

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| jable_show_detail_link | 在影片汇报中显示 Jable 详情页链接 | true |

### JM 本子

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| jm_client_type | jmcomic 客户端，api 或 html | api |
| jm_cookies | 浏览器请求中的完整 JM Cookie 字符串 | 空 |
| jm_client_domain | 自定义 JM 域名，逗号分隔 | 空 |
| jm_retry_times | 请求重试次数，0 使用默认值 | 0 |
| jm_use_proxy | 启用 JM 代理 | false |
| jm_proxy_url | HTTP 或 SOCKS5 代理地址 | 空 |
| jm_max_concurrent_photos | 并发章节数 | 3 |
| jm_max_concurrent_images | 并发图片数 | 5 |
| jm_search_page_size | 搜索和榜单显示数量 | 5 |
| jm_auto_delete_after_send | 发送 ZIP 后删除本地文件 | true |
| jm_reply_as_forward | JM 封面回复使用 OneBot 合并转发聊天记录 | false |

---

## ⚡ 性能基准

多图请求、来源拉取、Lolicon 代理下载与过审处理均已并行化。以下为模拟单次 HTTP 往返 100ms 下的 3 张图片耗时对比（可运行 `python benchmarks/benchmark.py` 复现）：

| 场景 | 串行 | 并发 | 加速比 |
| :--- | :--- | :--- | :--- |
| 自定义 API · 3 张 | 323.7ms | 108.3ms | ~3.0x |
| Nekos API · 3 张 | 323.3ms | 109.1ms | ~3.0x |
| Lolicon · 3 张下载 | 433.8ms | 219.1ms | ~2.0x |
| 过审处理 · 3 张 | 645.6ms | 303.9ms | ~2.1x |

---

## 🔗 数据来源

- [Lolicon API](https://api.lolicon.app)
- [Wallhaven](https://wallhaven.cc)
- [Jable](https://jable.tv)
- [MissAV](https://missav.ws)（作品搜索、详情和磁力链接）
- [Jina Reader](https://r.jina.ai)（读取受 Cloudflare 保护的公开页面）
- [Microlink](https://microlink.io)（封面占位时补充页面封面）
- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)（JM 搜索、详情与下载）
- 用户配置的自定义图片 API

---

## 👥 作者

- Echo

---

<div align="center">

**请遵守所在地法律法规、平台规则与群聊管理要求。**

</div>
