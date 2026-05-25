# astrbot_plugin_lyn

AstrBot 综合工具插件，包含 JM PDF 下载、Pixiv 图片私信、视频链接转换和视频下载功能。

## 指令

- `/jm <漫画ID>`：下载漫画并发送 PDF 文件。
- `/jm搜索 <关键词1> <关键词2>...`：搜索漫画信息。
- `/pixiv <PixivID/链接>`：通过 `pixiv.re` 获取作品图片，并私信给触发用户。当前仅支持 QQ OneBot v11，会把所有图片按顺序打包为一条合并转发消息。
- `/下载 <视频链接>`：使用 `yt-dlp` 下载视频，并作为文件发送。
- `/视频 <视频链接>`：使用 `yt-dlp` 下载视频，并作为视频消息发送。

## 自动转链接

收到 Bilibili、YouTube、Twitter/X、TikTok、抖音等视频平台链接或 QQ 小程序卡片时，插件会自动回复转换后的可复制链接。

B 站链接会优先转换为 `BV` 号链接，例如：

```text
https://www.bilibili.com/video/av170001
```

会转换为：

```text
https://www.bilibili.com/video/BV17x411w7KC
```

普通消息只会自动转链接，不会自动下载视频。只有明确使用 `/下载` 或 `/视频` 时才会下载。

## 依赖

- `jmcomic`：JM 下载。
- `img2pdf`：JM 图片转 PDF。
- `PyYAML`：生成 JM 配置。
- `yt-dlp`：视频平台解析和下载。
- `imageio-ffmpeg`：提供 FFmpeg，用于音视频合并。

## 数据目录

运行时文件会写入 AstrBot 数据目录：

```text
data/plugin_data/astrbot_plugin_lyn/
```

不会把运行时产物写入插件源码目录。

## 说明

- PDF 和视频文件发送依赖当前平台适配器对 `File`、`Video` 消息的支持。
- Pixiv 私信合并转发当前仅支持 QQ OneBot v11。
- 视频下载文件超过 100MB 时不会发送。
- 下载失败通常是网络、目标平台风控、依赖缺失或协议端发送限制导致。
- 同一个 JM 漫画 ID 的并发下载会串行执行。
- 插件启动后会每天清理一次超过 24 小时的 JM 下载任务目录。

## 命令变更

视频下载命令使用中文入口：

- `/下载`：替代 `/download`。
- `/视频`：替代 `/video`。
