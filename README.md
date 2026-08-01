# sd2api

把 TikTok Symphony Creative Studio 的 Seedance 文生视频与图生视频能力包装成两个本地 HTTP 接口：

- Seedance / ModelArk 风格：`/api/v3/contents/generations/tasks`
- OpenAI Videos 风格：`/v1/videos`

网页端实测基线：Dreamina Seedance 2.0、文生视频、单图图生视频与 Reference to video、5 秒；创建成功后异步轮询，最终返回可读取的 TikTok CDN MP4。每次 5 秒测试消耗 5 点，实际完成时间取决于 TikTok 队列。

> 这是对 TikTok 网页内部接口的非官方适配器。接口、内部模型 ID 和鉴权字段可能随网页更新而变化。只应用于你有权访问的账号，并遵守 TikTok 的服务条款。`sora-2` 在本项目中只是 OpenAI SDK 兼容别名，实际调用的是账号内的 Dreamina Seedance 2.0，不是 OpenAI 的 Sora 服务。

## 当前范围

已支持：

- 文生视频，Seedance 2.0（已实测）
- 单张首帧图生视频，Seedance 2.0（已实测）
- Reference to video：多图片、视频、音频混合参考
- OpenAI multipart 多文件上传，以及 OpenAI/Seedance 的远程 URL 和图片/音频 data URL
- Seedance 2.5 内部模型映射（未在本账号实测）
- 4–15 秒时长
- 创建、查询、列表、删除本地任务记录
- OpenAI 兼容状态对象和 MP4 流式下载
- SQLite 任务持久化
- 可选 Bearer API Key
- 登录账号浏览器 Profile 隔离与中央调度
- 自动发现 Client/Partner 子账号，并由用户选择哪些加入生成池
- 逐子账号显示 Seedance 2 权限、Credits 和检查错误
- 不同登录账号并行、同一登录账号下的子账号串行切换

暂未支持：

- OpenAI `file_id` 和严格的首帧+尾帧模式
- 取消已经提交到 TikTok 的任务（DELETE 只删除本地记录）
- 强制分辨率或画幅；网页的文生视频请求当前没有暴露这些参数

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

编辑 `.env`。在浏览器开发者工具的 Network 中找到一次成功的
`create_generate_task` 请求，手动复制以下值：

- 完整 `Cookie` 请求头到 `TIKTOK_COOKIE`
- 如果请求里存在且无法从 Cookie 自动识别，再填写 `x-csrftoken`、`x-creative-csrf-token` 和 `x-fp-id`

`device_id` 不需要也不能单独配置：浏览器池模式由 Chromium 登录会话自行维护；旧的 direct 模式只会尝试从 Cookie 中的会话设备字段自动派生。

不要把 `.env` 发给别人或提交到版本库。浏览器会话过期后需要重新复制。

启动：

```powershell
uvicorn sd2api.main:app --host 127.0.0.1 --port 8765
```

接口文档：`http://127.0.0.1:8765/docs`

### 不导出 Cookie：持久化浏览器模式

在 `.env` 中设置：

```dotenv
SD2API_MODE=browser
SD2API_BROWSER_CHANNEL=
SD2API_BROWSER_PROFILE=.browser-profile
```

服务启动后调用 `POST /browser/start`，程序会打开 Playwright 自带的独立 Chromium。只需在这个窗口登录 TikTok Ads；登录状态由 Chromium 自己保存在 `.browser-profile`，适配器不会读取或导出 Cookie。之后 `/v1/videos` 和 Seedance 端点会把任务加入队列并依次操作网页。

浏览器模式的限制：任务串行执行，容易受到网页 UI 更新影响，吞吐量低于直接 HTTP 模式。开发结束后可在该 Chromium 窗口登出；如需彻底移除本地会话，应先停止服务，再手动删除 `.browser-profile`。

## Docker / VPS 多账号池

Docker 模式在一个容器中运行 API、Chromium、Xvfb 和 noVNC。每个 TikTok Ads 账号使用独立目录 `/data/profiles/{account_id}`，账号注册信息和任务记录存放在 SQLite。无需导出 Cookie。

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

- 账号池面板：`http://127.0.0.1:8765/admin`
- noVNC：`http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale`
- API 文档：`http://127.0.0.1:8765/docs`

推荐在账号池面板中添加账号，只需填写 TikTok Ads 登录邮箱和密码。登录邮箱同时用于接收验证码，内部账号 ID 自动生成；加入号池后可在“编辑”中设置备注名称。密码经 Fernet 加密后才写入 SQLite，管理 API 和面板不会回传密码或密文。容器启动、账号掉线或点击“登录”时会自动执行账号密码登录，并通过 `cf_temp_mail` 的 `GET /api/emails?to_address=...` 获取本次登录产生的邮件验证码（支持纯数字及字母数字验证码）。

登录成功后程序会发现该登录身份下的全部 Client/Partner 子账号，并逐个检查 Dreamina Seedance 2.0 是否可选以及当前 Credits。子账号默认不加入生成池；管理员在面板中勾选一个或多个“SD2 可用”的子账号后才会参与调度。重新扫描会更新名称、权限和 Credits，但保留已有勾选结果。

图形验证码属于 TikTok 的交互式安全验证：程序会把账号状态标记为 `captcha_required` 并保持对应浏览器页面，管理员通过 noVNC 完成验证后，登录状态机会自动继续邮箱接码和后续登录。自动接码在后台并行进行，管理员仍可手动输入验证码；只要页面进入已登录状态，程序会立即确认成功。登录过程中关闭页面或 Chromium 后，程序最多自动重建三次并复用同一持久化 Profile。项目不包含验证码破解或绕过逻辑。

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

- 一个登录账号同一时间执行一个网页生成任务；其下多个子账号会在任务开始前自动切换，但不会在同一个 Chromium Profile 中并发操作。
- 不同账号并行执行；10 个在线账号的理论安全并发为 10。
- 同一登录账号勾选 5 个子账号不会把安全并发从 1 提升到 5；如需并行，必须使用不同登录 Profile。
- 调度器按当前负载最小优先分配；同负载时优先 credits 较多的账号，再做轮转。
- 页面可见 credits 为 0 的账号不会接收新任务；无法读取余额时仍可参与调度。
- `SD2API_POOL_MAX_PENDING` 限制全池等待与运行任务总量，超限返回 HTTP 429。
- 容器重启时最多同时启动 `SD2API_POOL_START_CONCURRENCY` 个浏览器，避免大量账号瞬间抢占 CPU 和内存。
- 已经在 TikTok 页面提交的任务不会自动换号重试，避免重复扣点；账号离线时只会停止接收新任务。
- 有运行或排队任务的账号不能被停用、停止或删除，管理 API 会返回 HTTP 409。
- 容器重建不会丢失账号登录 Profile 和已落库任务，但不要在生成过程中重启容器。

删除账号管理记录不会删除对应 Profile，响应中的 `profile_retained` 会明确标记这一点。需要彻底移除登录态时，应先停止账号，再由管理员单独清理相应的 volume 目录。

## Seedance 风格调用

```powershell
$headers = @{ Authorization = "Bearer change-me" }
$body = @{
  model = "seedance-2.0"
  content = @(@{ type = "text"; text = "一颗红球在白色桌面上缓慢滚动，固定镜头" })
  duration = 5
  ratio = "adaptive"
  resolution = "720p"
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

OpenAI 官方只列出部分固定时长；本适配器为了暴露 Seedance 网页能力，把 `seconds` 扩展为 4–15 的任意整数。

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
