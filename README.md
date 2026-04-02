# astrbot_plugin_memos_workspace_forwarder

`astrbot_plugin_memos_workspace_forwarder` 是一个给 AstrBot 用的 Memos 轮询转发插件。

它不走 RSS，而是直接使用带 `Bearer` 鉴权的 Memos API 拉取 memo，所以可以读取当前 token 有权限看到的内容，再转发到 QQ 群、频道或私聊。

现在默认还支持把新帖子渲染成一张“手记卡片图”发出去，卡片里会带头像、昵称、时间、正文和图片预览。

## 适用场景

- 你要推送 Memos 的工作区帖子
- 你还想一起推送公开帖子
- 你不想推送私有帖子
- 你希望能抓到“别人发的、但当前 token 可见”的帖子，而不只是 token 自己的帖子
- 你还希望把 memo 附件里的图片，或者正文里的图片 URL，一起转成图片消息发出去
- 你希望群里看到“谁谁谁发了新手记”并附上一张完整的手记卡片图

## 工作方式

插件会轮询：

- `GET /api/v1/memos`

并在请求头里带上：

```text
Authorization: Bearer <your_token>
```

推荐使用 Memos 的 `PAT`，也就是以 `memos_pat_` 开头的 Personal Access Token。

## 可见性说明

Memos API 当前使用这些可见性枚举：

- `PRIVATE` = 私有
- `PROTECTED` = 工作区
- `PUBLIC` = 公开

如果你要“推送工作区和公开，但不推送私有”，就把：

```text
visibility_mode = workspace_or_public
```

这会映射到：

```text
visibility in ["PROTECTED", "PUBLIC"]
```

## 作者过滤说明

- `creator_name` 是可选项
- 留空时：不筛作者，抓“当前 token 可见且符合 `visibility_mode` / `raw_filter` 条件”的全部帖子
- 填了时：只抓指定作者的帖子

所以如果你的目标是“推送整个工作区里所有可见的工作区帖和公开帖”，`creator_name` 应该留空。

## 图片转发说明

- 插件会优先提取 memo 的图片附件
- 也会尝试识别正文里的 Markdown 图片、HTML `<img>` 和直链图片 URL
- 对于 Memos 自己的 `/file/...` 图片，插件会带 `Bearer` token 下载后再转发，所以工作区图片也能发
- `forward_images = true` 时启用图片转发
- `max_images_per_memo` 控制每条 memo 最多转发多少张图片
- 如果你的反向代理没有把 `Authorization` 头转发给 `/file/*`，工作区附件图可能仍会下载失败，此时插件会回退为发送失败提示和原始地址

## 手记卡片图说明

- `render_memo_card = true` 时，插件会优先把帖子渲染成一张卡片图
- 卡片图会带作者头像、昵称、时间、可见性、正文和图片预览
- `announcement_template` 控制提示语，默认是 `{display_name} 发了新手记`
- `card_preview_image_count` 控制卡片图里最多展示多少张图片
- `standalone_images_when_card_enabled = false` 时，只发卡片图，不额外再把原图单独发一遍
- `standalone_images_when_card_enabled = true` 时，卡片图后面还会补发原始图片消息

## 配置

### 1. `sources[]`

- `id`：源唯一 ID
- `base_url`：Memos 站点地址
- `access_token`：Memos PAT 或 access token
- `creator_name`：可选，填写后只抓指定作者；留空时不筛作者
- `visibility_mode`
  - `workspace`
  - `protected`
  - `public`
  - `private`
  - `workspace_or_public`
  - `private_or_workspace`
  - `private_or_public`
  - `workspace_or_protected`
  - `all_mine`
- `raw_filter`：附加 CEL 过滤条件
- `page_size`：每页拉取数量，最大 1000
- `max_pages`：单次最多拉多少页
- `timeout`：HTTP 超时秒数

### 2. `targets[]`

- `id`
- `platform`
- `unified_msg_origin`
- `enabled`

`unified_msg_origin` 请使用 AstrBot 当前的会话字符串格式，例如 `aiocqhttp:GroupMessage:123456789`。
旧写法 `aiocqhttp:group:123456789` 现在插件也会自动兼容转换。

### 3. `jobs[]`

- `id`
- `source_ids[]`
- `target_ids[]`
- `interval_seconds`
- `batch_size`
- `enabled`

### 4. 顶层配置

- `dedup_ttl_seconds`
- `startup_delay_seconds`
- `summary_max_chars`
- `forward_images`
- `max_images_per_memo`
- `render_memo_card`
- `card_preview_image_count`
- `standalone_images_when_card_enabled`
- `announcement_template`
- `time_format`

## 最小示例

```json
{
  "sources": [
    {
      "id": "memos_workspace",
      "base_url": "https://memos.example.com",
      "access_token": "memos_pat_xxxxxxxxx",
      "creator_name": "",
      "visibility_mode": "workspace_or_public",
      "raw_filter": "",
      "page_size": 20,
      "max_pages": 3,
      "timeout": 15,
      "enabled": true
    }
  ],
  "targets": [
    {
      "id": "qq_group_a",
      "platform": "qq",
      "unified_msg_origin": "aiocqhttp:GroupMessage:123456789",
      "enabled": true
    }
  ],
  "jobs": [
    {
      "id": "memos_push",
      "source_ids": ["memos_workspace"],
      "target_ids": ["qq_group_a"],
      "interval_seconds": 300,
      "batch_size": 10,
      "enabled": true
    }
  ],
  "dedup_ttl_seconds": 604800,
  "startup_delay_seconds": 20,
  "summary_max_chars": 280,
  "forward_images": true,
  "max_images_per_memo": 4,
  "render_memo_card": true,
  "card_preview_image_count": 4,
  "standalone_images_when_card_enabled": false,
  "announcement_template": "{display_name} 发了新手记"
}
```

## 常用配置

只抓工作区：

```text
visibility_mode = workspace
```

抓工作区和公开，但不抓私有：

```text
visibility_mode = workspace_or_public
```

不筛作者，抓当前 token 可见的全部帖子：

```text
creator_name = ""
visibility_mode = all_mine
```

只抓指定作者：

```text
creator_name = "Xiyue"
visibility_mode = workspace_or_public
```

只抓带标签的 memo：

```text
raw_filter = "tag in [\"rss\"]"
```

关闭图片转发：

```text
forward_images = false
```

每条只转前两张图：

```text
max_images_per_memo = 2
```

启用卡片图但不补发原图：

```text
render_memo_card = true
standalone_images_when_card_enabled = false
```

修改提示语：

```text
announcement_template = "{display_name} 发了新手记"
```

## 命令

- `/memosws list`
- `/memosws status`
- `/memosws run [job_id]`
- `/memosws pause [job_id]`
- `/memosws resume [job_id]`
- `/memosws reset`

其中 `/memosws reset` 只清去重记录，不会删除插件配置。

## 依赖

核心功能只依赖：

- AstrBot 运行时
- Python 标准库
- Pillow

如果运行环境没有 Pillow，插件会自动回退到纯文本转发。

## 已知限制

- 当前版本按 `memo.name` 去重，所以同一条 memo 后续编辑不会重复推送
- 正文里的图片 URL 识别以常见 Markdown/HTML/直链写法为主，不是完整 Markdown 解析器
- 工作区附件图依赖站点的 `/file/*` 路由接受 Bearer 鉴权；如果部署层把 `Authorization` 头吃掉了，插件无法仅靠 PAT 拉取受保护附件
- 如果某个目标发送失败、另一个目标发送成功，这条 memo 会按“已发送”处理，更适合单目标或稳定目标场景
