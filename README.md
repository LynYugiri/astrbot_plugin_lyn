# AstrBot JM PDF Downloader

下载 JM 漫画，使用 `jmcomic` 的 `img2pdf` 插件转换为 PDF，并通过 AstrBot 文件消息发送。

## 指令

- `/jm <漫画ID>`：下载漫画并发送 PDF 文件。
- `/jm搜索 <关键词1> <关键词2>...`：搜索漫画信息。

## 数据目录

插件会把下载图片、PDF 和 `jm_option.yml` 写入 AstrBot 数据目录：

```text
data/plugin_data/astrbot_plugin_jm/
```

不会把运行时产物写入插件源码目录。

## 说明

PDF 发送依赖当前平台适配器对 `File` 消息的支持。下载失败时通常是网络、JM 域名可用性或 `jmcomic`、`img2pdf` 依赖问题。

同一个漫画 ID 的并发下载会串行执行。每次下载会写入独立任务目录，避免后续请求删除正在发送的 PDF。

插件启动后会每天清理一次超过 24 小时的漫画下载任务目录。
