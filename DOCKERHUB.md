# Docker Hub 自动发布

仓库内的 GitHub Actions 工作流会在以下情况自动构建并推送镜像：

- `main` 分支有新提交
- 推送版本标签，支持 `1.2.0` 和 `v1.2.0` 两种格式
- 在 GitHub Actions 页面手动运行

每个镜像标签都是同时包含 `linux/amd64` 和 `linux/arm64` 的多架构清单，x86 和 ARM VPS 使用同一个镜像地址即可。

## 首次配置

先在 Docker Hub 创建名为 `sd2api` 的仓库，再创建一个只用于 CI 的 Personal Access Token。

然后打开 GitHub 仓库的 `Settings` → `Secrets and variables` → `Actions`，添加：

- `DOCKERHUB_USERNAME`：Docker Hub 用户名，当前镜像名预设为 `coolqoo/sd2api`
- `DOCKERHUB_TOKEN`：Docker Hub Personal Access Token，不要使用账户密码

工作流会发布这些常用标签：

- `coolqoo/sd2api:latest`：`main` 分支或最新发布标签的版本
- `coolqoo/sd2api:build-20260810-12`：带构建日期和 GitHub Actions 流水线序号的可追溯版本
- `coolqoo/sd2api:1.2.0`、`1.2`、`1`：推送 `v1.2.0` 标签时生成

工作流不再生成含义重复的 `main` 和不易阅读的纯 `sha-*` 新标签。Docker Hub 中已有的旧标签不会被自动删除，可在确认无人使用后从 Docker Hub 标签页面手动删除。

## VPS 更新

无论 VPS 是 x86_64 还是 ARM64，都使用相同命令：

```bash
docker pull coolqoo/sd2api:latest
docker compose up -d --pull always --no-build
```

项目的 `docker-compose.yml` 已将默认镜像设置为 `coolqoo/sd2api:latest`。如需固定版本，可在 `.env.docker` 同级目录创建 Compose 使用的 `.env` 文件：

```dotenv
SD2API_IMAGE=coolqoo/sd2api:1.2.0
```

注意，Compose 的 `.env` 用于替换镜像变量，应用自身的敏感环境变量仍保存在 `.env.docker`。
