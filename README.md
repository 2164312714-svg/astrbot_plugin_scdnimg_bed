# scdn 图床插件

基于 [img.scdn.io](https://img.scdn.io) API 的 AstrBot 图床插件，支持上传图片、通过 URL 上传、查询图片公开元数据，以及解析 scdn 图片链接并发图到群里。

## 功能一览

| 命令 | 作用 | 支持的图片来源 |
|------|------|----------------|
| `/图床上传` | 上传图片到 scdn 图床 | 附带图片、回复/引用图片消息、图片 URL、本地文件路径、base64 data URI |
| `/图床链接` | 通过远程图片 URL 上传 | HTTP/HTTPS 图片链接 |
| `/图床查询` | 查询图片公开元数据 | 图片文件名、ID，或完整 scdn URL |
| `/图床解析` | 解析 scdn 链接并把图片发回群里 | 完整 scdn URL |
| `/图床帮助` | 显示命令帮助 | - |

## 安装方法

### 方式一：通过 AstrBot 插件市场（推荐）

等待本插件上架 AstrBot 插件市场后，直接在管理界面搜索 `scdnimg-bed` 或 `astrbot_plugin_scdnimg_bed` 安装。

### 方式二：手动上传 zip

1. 打开本仓库的 [Releases](https://github.com/2164312714-svg/astrbot_plugin_scdnimg_bed/releases) 页面。
2. 下载最新版本的 `astrbot_plugin_scdnimg_bed.zip`。
3. 进入 AstrBot 管理后台 → 插件 → 安装插件 → 上传本地插件包。
4. 上传完成后，点击“重载插件”或重启 AstrBot。

### 方式三：通过 Git 链接安装

如果你的 AstrBot 支持通过 Git 仓库安装，可填写：

```
https://github.com/2164312714-svg/astrbot_plugin_scdnimg_bed
```

## 配置说明

安装完成后，在 AstrBot 插件配置页面找到 `astrbot_plugin_scdnimg_bed` 进行配置：

| 配置项 | 类型 | 说明 | 默认值 |
|--------|------|------|--------|
| `api_base_url` | 字符串 | scdn 图床 API 端点地址 | `https://img.scdn.io/api/v1.php` |
| `default_cdn_domain` | 字符串 | 默认 CDN 域名，决定返回的图片链接域名 | `img.scdn.io` |
| `default_storage` | 枚举 | 默认存储后端 | `local` |
| `default_output_format` | 枚举 | 默认输出格式，`auto` 表示由服务端自动处理 | `auto` |
| `timeout` | 整数 | 单次请求超时时间，单位秒 | `60` |
| `local_upload_enabled` | 布尔 | 是否允许通过本地文件路径上传（安全开关，默认关闭） | `false` |
| `local_upload_root` | 字符串 | 本地文件上传白名单根目录，留空表示不限制 | `""` |

### 可选 CDN 域名

- `img.scdn.io`
- `cloudflareimg.cdn.sn`
- `edgeoneimg.cdn.sn`
- `esaimg.cdn1.vip`
- `cloudflarecnimg.scdn.io`
- `anycastimg.scdn.io`
- `edgeoneimg.cdn1.vip`

### 存储后端说明

- `local`：默认本地存储
- `telegram`：上传到 Telegram
- `r2`：Cloudflare R2 对象存储

## 命令详解

### 1. 上传图片 `/图床上传`

最通用的上传命令，支持多种图片来源。

```
/图床上传 [图片URL] [--format=webp] [--cdn=img.scdn.io] [--storage=local] [--password=密码]
```

#### 使用场景

**场景 A：直接附带图片**

在发送命令的同时附带一张图片：

```
/图床上传
```

**场景 B：回复/引用图片消息**

回复一条包含图片的消息：

```
/图床上传
```

已适配平台：
- `aiocqhttp`（QQ / NapCat）
- `telegram`
- 其他平台会尝试从消息 raw 数据兜底解析

**场景 C：提供图片 URL**

```
/图床上传 https://example.com/image.png --format=webp --storage=r2
```

**场景 D：本地文件路径**

> 出于安全考虑默认关闭，需在插件配置中开启 `local_upload_enabled`，并可设置 `local_upload_root` 限制可读目录。

```
/图床上传 C:\Users\Me\Pictures\cat.png
```

#### 可用参数

- `--format`：输出格式
  - 可选：`auto`、`jpg`、`jpeg`、`png`、`webp`、`gif`、`webp_animated`
- `--cdn`：指定 CDN 域名
- `--storage`：指定存储后端，`local` / `telegram` / `r2`
- `--password`：设置图片访问密码

### 2. 通过 URL 上传 `/图床链接`

专门用于上传远程图片链接。

```
/图床链接 <图片URL> [--format=webp] [--cdn=img.scdn.io] [--storage=local]
```

示例：

```
/图床链接 https://example.com/image.png --format=webp
```

### 3. 查询图片信息 `/图床查询`

查询已上传图片的公开元数据。

```
/图床查询 <图片ID或文件名>
/图床查询 <scdn图片URL>
```

示例：

```
/图床查询 abc123.png
/图床查询 https://img.scdn.io/i/abc123.png
```

当传入完整 scdn URL 时，插件会自动提取文件名进行查询。

### 4. 解析 scdn 链接并发图 `/图床解析`

当群里有人发了 scdn 图片链接，想让机器人把图片重新发出来时：

```
/图床解析 <scdn图片URL>
```

示例：

```
/图床解析 https://img.scdn.io/i/abc123.png
```

机器人会：
1. 解析链接中的图片标识
2. 查询图片元数据
3. 把原图发回群里，并附带文件名、大小等简要信息

## 命令别名

| 主命令 | 别名 |
|--------|------|
| `/图床上传` | `/上传图床`、`/scdn-upload` |
| `/图床链接` | `/上传图床链接`、`/scdn-url` |
| `/图床查询` | `/查询图床`、`/scdn-info` |
| `/图床解析` | `/解析图床`、`/scdn-parse`、`/scdn-send` |
| `/图床帮助` | `/scdn-help` |

## 常见问题

### Q1：为什么回复图片后提示“请发送/回复一张图片”？

可能原因：
- 你使用的消息平台适配器尚未专门适配，插件无法从回复消息中提取图片。
- 当前平台的消息段格式与插件不兼容。

解决办法：
- 直接附带图片发送命令，而不是回复图片。
- 如果必须回复图片，请告诉我你使用的平台（如 QQ、Telegram、微信等），我可以继续针对性适配。

### Q2：`/图床链接` 传入 scdn 自己的 URL 为什么会失败？

`/图床链接` 用于把**新的远程图片**上传到 scdn。如果传入的是 scdn 已有的图片 URL，API 可能会拒绝或返回 400。此时应使用 `/图床查询` 查询信息，或使用 `/图床解析` 把图片发出来。

### Q3：上传后返回的 URL 打不开？

检查 `default_cdn_domain` 配置是否正确。某些 CDN 域名可能需要特定网络环境才能访问。可尝试切换为 `img.scdn.io` 或其他可用域名。

### Q4：如何设置图片密码？

上传时使用 `--password` 参数：

```
/图床上传 https://example.com/secret.png --password=123456
```

## 平台兼容性

| 功能 | aiocqhttp (QQ/NapCat) | Telegram | 其他平台 |
|------|----------------------|----------|----------|
| 附带图片上传 | ✅ | ✅ | ✅ |
| 回复图片上传 | ✅ | ✅ | 兜底兼容 |
| URL 上传 | ✅ | ✅ | ✅ |
| 查询图片 | ✅ | ✅ | ✅ |
| 解析链接发图 | ✅ | ✅ | ✅ |

## 返回值示例

### 上传成功

```
上传成功！
URL: https://img.scdn.io/i/abc123.webp
文件名: abc123.webp
存储: local
大小: 1024 KB -> 256 KB
压缩比: 25%
```

### 查询成功

```
图片信息：
ID: abc123
文件名: abc123.webp
大小: 256 KB
上传时间: 2026-06-20 12:00:00
存储后端: local
图片URL: https://img.scdn.io/i/abc123.webp
```

## 更新日志

### v1.0.0

- 支持 `/图床上传` 附带图片、回复图片、URL、本地路径、base64 data URI 上传
- 支持 `/图床链接` 远程 URL 上传
- 支持 `/图床查询` 查询图片元数据
- 支持 `/图床解析` 解析 scdn 链接并发图
- 支持命令别名
- 支持配置默认 CDN、存储后端、输出格式、超时时间

## 依赖

本插件仅依赖 AstrBot 内置 API，无需额外安装 Python 包。

## 仓库与作者

- 仓库：[https://github.com/2164312714-svg/astrbot_plugin_scdnimg_bed](https://github.com/2164312714-svg/astrbot_plugin_scdnimg_bed)
- 作者：Clove.
