# sd2api

把 TikTok Symphony Creative Studio 的 Seedance 文生视频与图生视频能力包装成两个本地 HTTP 接口：

- Seedance / ModelArk 风格：`/api/v3/contents/generations/tasks`
- OpenAI Videos 风格：`/v1/videos`

协议端实测基线：Dreamina Seedance 2.0 的文生视频、单图图生视频、2 图 + 视频 + 音频的 Reference to video，均使用 5 秒任务完成了素材上传、创建、轮询和原始 MP4 下载。Chromium 只在登录、图形验证码和条款确认时启动；登录完成后生成服务不依赖浏览器 UI。每次 5 秒测试消耗 5 点，实际完成时间取决于 TikTok 队列。

> 这是对 TikTok 网页内部接口的非官方适配器。接口、内部模型 ID 和鉴权字段可能随网页更新而变化。只应用于你有权访问的账号，并遵守 TikTok 的服务条款。`sora-2` 在本项目中只是 OpenAI SDK 兼容别名，实际调用的是账号内的 Dreamina Seedance 2.0，不是 OpenAI 的 Sora 服务。

## 当前范围

已支持：

- 文生视频，Seedance 2.0（已实测）
- 单张首帧图生视频，Seedance 2.0（已实测）
- Reference to video：多图片、视频、音频混合参考（已实测）
- OpenAI multipart 多文件上传，以及 OpenAI/Seedance 的远程 URL 和图片/音频 data URL
- Seedance 2.5 内部模型映射（未在本账号实测）
- 4–15 秒时长
- 创建、查询、列表、删除本地任务记录
- OpenAI 兼容状态对象和 MP4 流式下载
- SQLite 任务持久化
- 可选 Bearer API Key
- 登录账号浏览器 Profile 隔离、加密协议会话与中央调度
- 自动发现 Client/Partner 子账号，并由用户选择哪些加入生成池
- 逐子账号显示 Seedance 2 权限、Credits 和检查错误
- 每个子账号独立 CookieJar；同一登录账号下多个已启用子账号也可并发

暂未支持：

- OpenAI `file_id` 和严格的首帧+尾帧模式
- 取消已经提交到 TikTok 的任务（DELETE 只删除本地记录）
- 自定义分辨率或画幅；网页生成请求当前没有暴露这些参数

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

推荐设置 `SD2API_MODE=browser_pool`，再通过 `/admin` 添加 TikTok Ads 登录邮箱和密码。程序会在 Chromium 中完成账号密码和邮箱验证码步骤，只把图形验证码交给管理员；登录成功后自动捕获 Cookie、CSRF、fp ID、Client Hints 和 device ID，加密写入 SQLite 并关闭 Chromium。用户不需要查看或填写 Cookie、fp ID、device ID，也不要把 `.env` 发给别人或提交到版本库。

启动：

```powershell
uvicorn sd2api.main:app --host 127.0.0.1 --port 8765
```

接口文档：`http://127.0.0.1:8765/docs`

### 旧 UI 浏览器模式

在 `.env` 中设置：

```dotenv
SD2API_MODE=browser
SD2API_BROWSER_CHANNEL=
SD2API_BROWSER_PROFILE=.browser-profile
```

`SD2API_MODE=browser` 保留为 UI 自动化回退模式：调用 `POST /browser/start` 后，任务会依次操作网页。新部署应使用 `browser_pool`，它在登录后将会话加密保存，扫描、上传、生成、轮询和下载都直接调用协议，不受生成页 DOM 变化影响。

浏览器模式的限制：任务串行执行，容易受到网页 UI 更新影响，吞吐量低于直接 HTTP 模式。开发结束后可在该 Chromium 窗口登出；如需彻底移除本地会话，应先停止服务，再手动删除 `.browser-profile`。

## Docker / VPS 多账号池

Docker 模式在一个容器中运行 API、按需 Chromium、Xvfb 和 noVNC。每个 TikTok Ads 登录账号使用独立 Profile；账号凭据、加密协议会话、子账号选择和任务记录存放在 SQLite。Chromium 登录完成后自动关闭，正常生成时不会常驻，也无需导出 Cookie。

准备配置：

```bash
cp .env.docker.example .env.docker
```

至少修改这些值：

```dotenv
SD2API_API_KEY=调用视频 API 的长随机密钥
SD2API_ADMIN_KEY=管理账号池的另一条长随机密钥
NOVNC_PASSWORD=noVNC 登录密码
SD2API_CREDENTIAL_KEY=用于加密账号密码的长期随机密钥
SD2API_TEMP_MAIL_BASE_URL=https://你的-cf_temp_mail-worker
SD2API_TEMP_MAIL_API_KEY=cf_temp_mail 的 API_SECRET
```

启动：

```bash
docker compose up -d --build
docker compose ps
```

Compose 默认只绑定 VPS 的 `127.0.0.1`。从本机建立 SSH 隧道：

```bash
ssh -L 8765:127.0.0.1:8765 -L 6080:127.0.0.1:6080 user@your-vps
```

然后打开：

- 管理控制台：`http://127.0.0.1:8765/admin`
- noVNC：`http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale`
- API 文档：`http://127.0.0.1:8765/docs`

管理控制台按功能分为四个菜单：

- **生视频**：直接创建 T2V、单首帧 I2V 和多模态 R2V 任务，显示可用 Credits、预计消耗和当前任务状态。
- **号池管理**：添加登录账号、处理登录、扫描 Client/Partner 子账号，并选择哪些有 Seedance 权限的子账号参与调度。
- **日志**：查看持久化的系统、账号、登录和视频事件，可按级别、分类及关键词筛选。
- **视频管理**：搜索和筛选全部任务，页面打开时会主动刷新排队中与生成中任务的 TikTok 状态，并支持下载成功视频或删除本地记录。

控制台使用 `SD2API_ADMIN_KEY` 登录；配置了独立的 `SD2API_API_KEY` 时，Admin Key 也可以从控制台调用视频生成与下载端点。删除视频任务只删除本地记录，不会取消已经提交到 TikTok 的上游任务。

推荐在账号池面板中添加账号，只需填写 TikTok Ads 登录邮箱和密码。登录邮箱同时用于接收验证码，内部账号 ID 自动生成；加入号池后可在“编辑”中设置备注名称。密码经 Fernet 加密后才写入 SQLite，管理 API 和面板不会回传密码或密文。容器启动、账号掉线或点击“登录”时会自动执行账号密码登录，并通过 `cf_temp_mail` 的 `GET /api/emails?to_address=...` 获取本次登录产生的邮件验证码（支持纯数字及字母数字验证码）。

登录成功后程序保存加密协议会话，并通过 TikTok 的结构化 JSON 接口读取全部 Client/Partner 子账号、Dreamina Seedance 2.0 权限、用户层级与 Credits，不再展开账号菜单或操作模型下拉框。每个子账号使用独立 CookieJar 和公开账号上下文 `s_aio_client_id`；Cookie、CSRF、fp ID、Client Hints 和 device ID 都从登录会话自动获得，不对管理 API 或用户配置暴露。子账号默认不加入生成池；管理员在面板中勾选一个或多个“SD2 可用”的子账号后才会参与调度。重新扫描会更新名称、权限和 Credits，但保留已有勾选结果。

协议请求由 `curl_cffi` 使用 Chrome TLS/HTTP2 指纹发送；素材上传支持 ImageX/VOD 直传和分片，不需要后台保留 Chromium 进程。会话密文只使用 `SD2API_CREDENTIAL_KEY`（或回退的 Admin Key）解密，管理 API 永远不会返回 Cookie、密码或密文。

图形验证码属于 TikTok 的交互式安全验证：程序会把账号状态标记为 `captcha_required` 并保持对应浏览器页面，管理员通过 noVNC 完成验证后，登录状态机会自动继续邮箱接码和后续登录。自动接码在后台并行进行，管理员仍可手动输入验证码；只要页面进入已登录状态，程序会立即确认成功。登录过程中关闭页面或 Chromium 后，程序最多自动重建三次并复用同一持久化 Profile。项目不包含验证码破解或绕过逻辑。

`SD2API_AUTO_ACCEPT_TERMS=true` 表示部署者明确授权程序在首次使用子账号时滚动阅读提示并自动点击 TikTok Creative GenAI Terms 的 “Accept”。默认值为 `false`；关闭时程序会暂停，并要求管理员通过 Chromium/noVNC 自行审阅和接受。

也可以通过 API 添加账号：

```bash
curl -X POST http://127.0.0.1:8765/admin/accounts \
  -H "Authorization: Bearer $SD2API_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"username":"login@example.com","password":"your-password","auto_login":true,"start":true}'
```

为避免密码进入 shell history，实际部署优先使用账号池面板。继续添加 `account_002`、`account_003` 即可；noVNC 底部任务栏用于切换窗口，也可以调用聚焦端点：

```bash
curl -X POST http://127.0.0.1:8765/admin/accounts/account_002/focus \
  -H "Authorization: Bearer $SD2API_ADMIN_KEY"
```

查看账号池：

```bash
curl http://127.0.0.1:8765/admin/pool/status \
  -H "Authorization: Bearer $SD2API_ADMIN_KEY"
```

返回值包含每个登录账号的 `enabled`、`running`、`logged_in`、`login_state`、`busy`、`queued` 和 `subaccounts`。每个子账号包含 `advertiser_id`、`account_type`、`enabled`、`seedance_access`、`credits` 和检查错误；汇总状态还包含 `max_parallel`、`enabled_subaccounts`、`logging_in` 和 `captcha_required`。

### 并发规则

- 协议模式不切换网页上下文；每个已启用子账号都有隔离的 CookieJar，可与同一登录账号下的其他子账号并发。
- `max_parallel` 为当前在线且可用的子账号数；例如一个登录账号启用 5 个有权限的子账号，可并行分配到这 5 个上下文。
- 素材上传在每个协议客户端内受 `SD2API_PROTOCOL_UPLOAD_CONCURRENCY` 限制；大文件超过 `SD2API_PROTOCOL_DIRECT_UPLOAD_BYTES` 后按 `SD2API_PROTOCOL_SLICE_BYTES` 分片。
- 调度器按当前负载最小优先分配；同负载时优先 credits 较多的账号，再做轮转。
- 页面可见 credits 为 0 的账号不会接收新任务；无法读取余额时仍可参与调度。
- `SD2API_POOL_MAX_PENDING` 限制全池等待与运行任务总量，超限返回 HTTP 429。
- 容器重启时最多并发验证 `SD2API_POOL_START_CONCURRENCY` 个账号；有效协议会话不会启动浏览器，只有缺失或过期时才拉起对应 Chromium。
- 已经在 TikTok 页面提交的任务不会自动换号重试，避免重复扣点；账号离线时只会停止接收新任务。
- 有运行或排队任务的账号不能被停用、停止或删除，管理 API 会返回 HTTP 409。
- 容器重建不会丢失账号登录 Profile、加密协议会话和已落库任务；已提交任务可在重启后继续查询。

删除账号管理记录不会删除对应 Profile，响应中的 `profile_retained` 会明确标记这一点。需要彻底移除登录态时，应先停止账号，再由管理员单独清理相应的 volume 目录。

## 生成参数说明

以下结论来自 2026-08-02 对当前 TikTok Symphony Creative Studio 页面、实际创建请求和 T2V/I2V/R2V 成功任务的交叉验证。TikTok 内部网页接口可能更新，升级后应重新核对。

| 能力 | Seedance 风格字段 | OpenAI 风格字段 | 当前行为 |
|---|---|---|---|
| 模型 | `model` | `model` | `seedance-2.0` 已实测；`sora-2` 只是它的 OpenAI 兼容别名。代码包含 Seedance 2.5 映射，但当前账号网页没有展示 2.5，因此未实测且仍取决于账号权限。网页还展示 `Video 1.5 Pro`，本适配器不支持。 |
| 时长 | `duration` | `seconds` | **真实生效**。支持 4–15 秒的任意整数，步长 1；会写入 TikTok 请求正文和 `settings`。当前页面显示 Seedance 2.0 的预估 Credits 与秒数相同，例如 4 秒为 4 Credits、15 秒为 15 Credits。 |
| 分辨率 | `resolution` | `size` | **不控制上游**。TikTok 页面没有分辨率控件，请求也不包含这些字段。当前适配器只保存并回显它们，方便兼容 SDK。 |
| 画幅 | `ratio` | 包含在 `size` 的宽高方向中 | **不控制上游**。页面没有画幅控件，请求不包含该字段。 |
| 随机种子 | `seed` | 无 | TikTok 页面没有对应控件；当前仅作为兼容字段接收，不会发给上游。 |
| 固定镜头 | `camera_fixed` | 无 | Seedance 2.0 没有相机控制开关；页面所示 Camera control 属于另一个 `Video 1.5 Pro` 模型。当前字段不会发给上游。需要固定镜头时只能写进 prompt。 |
| 水印 | `watermark` | 无 | 页面没有生成水印开关。TikTok 同时返回原始 VID 与 watermark VID，适配器固定下载原始 VID；字段不会发给上游。 |
| 音频生成 | `generate_audio` | 无 | Seedance 2.0 在页面中标注 `Includes audio`，但没有开关；是否包含音频由模型决定，该字段不能启用或禁用音频。R2V 仍可上传参考音频。 |
| 提示词增强 | 无 | 无 | 当前网页请求固定发送 `useEnhancePrompt=false`；适配器没有暴露开关。 |
| 首尾帧 | `role=first_frame/last_frame` | `input_reference` 仅单图 | 网页支持 First frame only 和 First and last frame；当前适配器只实现单首帧。首尾帧双图请求会返回 HTTP 501。 |

当前实测的三类任务都返回了 `720 × 1280` 的原始视频：T2V 即使请求记录中写入 `ratio=16:9`，以及 I2V/R2V 即使 OpenAI 请求写入 `size=1280x720`，最终仍为竖屏 `720 × 1280`。其中 I2V 输入图是 `1254 × 1254` 方图，也没有改变输出画幅。因此，在 TikTok 网页增加相应控件并确认协议字段之前，应把输出视为固定的竖屏 720p；不要依赖 `ratio`、`resolution` 或 `size` 改变结果。

分辨率/画幅兼容字段仍会出现在任务响应中，它们表示调用方提交的记录值，不代表实际 MP4 尺寸。下载端点优先返回 TikTok 的 original video（本轮实测为 `720 × 1280`），而不是 360p/480p/540p 转码版本。

## Seedance 风格调用

```powershell
$headers = @{ Authorization = "Bearer change-me" }
$body = @{
  model = "seedance-2.0"
  content = @(@{ type = "text"; text = "一颗红球在白色桌面上缓慢滚动，固定镜头" })
  duration = 5
} | ConvertTo-Json -Depth 5

$task = Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/api/v3/contents/generations/tasks `
  -Headers $headers `
  -ContentType application/json `
  -Body $body

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8765/api/v3/contents/generations/tasks/$($task.id)" `
  -Headers $headers
```

成功状态为 `succeeded`，视频地址位于 `content.video_url`。

图生视频在 `content` 中增加一个 `image_url` 项。URL 必须是公网 HTTP(S) 图片，也可使用 `data:image/...;base64,...`：

```json
{
  "model": "seedance-2.0",
  "content": [
    {"type": "text", "text": "The blue cube slowly rotates"},
    {"type": "image_url", "image_url": {"url": "https://example.com/cube.png"}}
  ],
  "duration": 5
}
```

Reference to video 使用 Seedance 多模态 `content`。图片角色为 `reference_image`，视频角色为 `reference_video`，音频角色为 `reference_audio`：

```json
{
  "model": "seedance-2.0",
  "content": [
    {"type": "text", "text": "Use Image 1 and Image 2 as subjects, follow Video 1, and use Audio 1."},
    {"type": "image_url", "image_url": {"url": "https://example.com/cube.png"}, "role": "reference_image"},
    {"type": "image_url", "image_url": {"url": "https://example.com/sphere.png"}, "role": "reference_image"},
    {"type": "video_url", "video_url": {"url": "https://example.com/motion.mp4"}, "role": "reference_video"},
    {"type": "audio_url", "audio_url": {"url": "https://example.com/tone.wav"}, "role": "reference_audio"}
  ],
  "duration": 5
}
```

当前按 Seedance 参考输入上限校验：最多 9 张图片、3 个视频、3 段音频；音频不能单独使用，至少还要有一张图片或一个视频。

## OpenAI 兼容调用

原生 HTTP（JSON 与 `multipart/form-data` 都可用）：

```powershell
$headers = @{ Authorization = "Bearer change-me" }
$body = @{
  model = "sora-2"
  prompt = "A red ball rolls slowly across a clean white tabletop, static camera."
  seconds = 5
  size = "720x1280"
} | ConvertTo-Json

$video = Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/v1/videos `
  -Headers $headers `
  -ContentType application/json `
  -Body $body

Invoke-RestMethod -Uri "http://127.0.0.1:8765/v1/videos/$($video.id)" -Headers $headers
Invoke-WebRequest -Uri "http://127.0.0.1:8765/v1/videos/$($video.id)/content" -Headers $headers -OutFile result.mp4
```

OpenAI multipart 图生视频：

```powershell
curl.exe -X POST http://127.0.0.1:8765/v1/videos `
  -H "Authorization: Bearer change-me" `
  -F "model=sora-2" `
  -F "prompt=The blue cube slowly rotates" `
  -F "seconds=5" `
  -F "size=720x1280" `
  -F "input_reference=@C:\path\to\cube.png;type=image/png"
```

OpenAI 官方的 `input_reference` 是单个图片对象。为了在同一路径上暴露 TikTok Reference to video，本项目增加了可重复的 multipart `reference_media` 字段（也可以分别使用 `reference_image`、`reference_video`、`reference_audio`）：

```powershell
curl.exe -X POST http://127.0.0.1:8765/v1/videos `
  -H "Authorization: Bearer change-me" `
  -F "model=sora-2" `
  -F "prompt=Use Image 1 and Image 2 as subjects, follow Video 1, and use Audio 1." `
  -F "seconds=5" `
  -F "reference_media=@C:\path\to\cube.png;type=image/png" `
  -F "reference_media=@C:\path\to\sphere.png;type=image/png" `
  -F "reference_media=@C:\path\to\motion.mp4;type=video/mp4" `
  -F "reference_media=@C:\path\to\tone.wav;type=audio/wav"
```

JSON 调用可使用扩展字段 `references`，其中元素结构与上面的 Seedance 多模态内容一致。`input_reference` 和 `references/reference_media` 互斥。

支持 JPEG、PNG、WebP、BMP、TIFF、GIF、MP4、MOV、WAV 和 MP3。默认图片上限 30 MiB/4000 万像素，视频上限 200 MiB，音频上限 15 MiB。远程素材会阻止私网和本机地址；视频 data URL 不接受，图片和音频可以使用 data URL。暂存文件会在浏览器任务结束后自动清理。素材模式需要 `SD2API_MODE=browser` 或 `browser_pool`。

OpenAI Python SDK 可以通过自定义 `base_url` 使用创建和查询接口（SDK 版本需包含 Videos 资源）：

```python
from openai import OpenAI

client = OpenAI(api_key="change-me", base_url="http://127.0.0.1:8765/v1")
video = client.videos.create(
    model="sora-2",
    prompt="A red ball rolls across a white tabletop.",
    seconds="5",
    size="720x1280",
)
print(video.id, video.status)
```

OpenAI 官方只列出部分固定时长；本适配器为了暴露 Seedance 网页能力，把 `seconds` 扩展为 4–15 的任意整数。`size` 保留用于 OpenAI SDK 兼容，但如上表所述，它当前不会改变 TikTok 输出尺寸。

## 状态映射

| TikTok 任务状态 | Seedance 响应 | OpenAI 响应 |
|---|---|---|
| 等待 | `queued` | `queued` |
| 生成或渲染 | `running` | `in_progress` |
| 生成与渲染都成功，且存在视频 ID | `succeeded` | `completed` |
| 任一阶段失败 | `failed` | `failed` |

TikTok 会分别报告生成状态和渲染状态。本适配器只有在两个阶段都成功并拿到视频 ID 后才报告完成。

## 测试

单元测试全部使用模拟上游，不会消耗 TikTok 点数：

```powershell
pytest
```
