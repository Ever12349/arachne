# Admin（0.12.0）

给人用的本机后台，挂在同一个 FastAPI 进程的 `/admin`。Agent 契约不变：仍然只要求 `POST /extract`。没有公开的 `/browse`。`/admin` 与 `/admin/api/*` 不进 OpenAPI，调用方不要依赖它们。

点「开始」会在 **Mac 宿主机**上打开有头浏览器。页面不再把「复制命令」当作主路径。容器里的进程不能显示 Mac 的窗口，所以浏览器由只监听 `127.0.0.1` 的宿主机助手打开。

不解验证码，不代登录。人通过墙之后点「完成」，或者挑战页自己消失后脚本保存 Cookie。

## 一次性安装

在放仓库的那台 Mac 上做一次。之后每次新建会话不用再开终端。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium
python scripts/install_host_helper.py
```

安装脚本会：

- 在 `.env` 里写入 `ARACHNE_HOST_HELPER_TOKEN`（已有则保留）和 `ARACHNE_SESSION_KEY`（已有则保留）。文件权限 `0600`。终端不打印这两个值。
- 写 `~/Library/LaunchAgents/com.arachne.host-helper.plist`，并 `launchctl bootstrap` 到当前图形会话。助手只绑定 `127.0.0.1:18765`。
- 把 `ARACHNE_SESSIONS_DIR` 设成仓库里的 `data/sessions` 绝对路径，和 Compose 挂载的 `./data` 是同一目录。

不是 macOS 时，脚本只写文件，不调用 launchd。前台启动：

```bash
scripts/arachne-host-helper
```

Docker Compose 会从项目 `.env` 读 `ARACHNE_HOST_HELPER_TOKEN` 和 `ARACHNE_SESSION_KEY`。改完 `.env` 后重新创建容器，让管理进程和助手用同一对值。容器内不要跑这个助手，也不要设 `ARACHNE_CAPTURE_MODE=host`。

## 一键流程

1. 在本机打开 `http://127.0.0.1:8000/admin`。直接跑 uvicorn 时，TCP 对端必须是 loopback。Compose 见下面的 `ARACHNE_ADMIN_TRUST_DOCKER`。
2. 软墙失败（`challenge_detected` 或 `unauthorized_upstream`）出现在仪表盘和「最近失败」里。URL 是链接。
3. 点 URL 或「为此建会话」。「新建会话」已经填好这个 URL。
4. 点「开始」。宿主机打开有头 Chromium。人通过验证码或登录墙。脚本不会替你解题。
5. 点「完成」。若挑战页标记消失且这个主机已经有 Cookie，助手也会自己写入，不必再按终端。
6. 页面轮询 `auto-<host>.bin`。Compose 把 `./data` 挂进容器，所以宿主机写下的文件服务进程能看见。
7. 出现「重试抽取」后提交。同一条抽取管道会带上这个会话。

管理进程若跑在宿主机上且助手没启动，`ARACHNE_CAPTURE_MODE=auto`（默认）会在本进程里打开浏览器，仍然不用复制命令。`ARACHNE_CAPTURE_MODE=helper` 时从不在管理进程里开浏览器。容器里（`ARACHNE_IN_DOCKER=true`，Compose 已设置）永远不在容器里开窗口：浏览器里的页面会把「开始」转到 `http://127.0.0.1:18765`，因为浏览器在 Mac 上，能访问宿主机的 loopback。

没有失败记录时，才在「新建会话」里手贴 URL，或贴扁平 Cookie JSON（`{"sid":"abc"}`），走和 `scripts/write_session.py` 一样的 Fernet 写入。

## 助手

`scripts/arachne-host-helper`（实现在 `scripts/host_helper.py`）只听 `127.0.0.1`。绑到其它地址会直接退出。

| 方法 | 路径 | 鉴权 |
|------|------|------|
| GET | `/health` | 仅 loopback，不需要令牌 |
| POST | `/capture` | loopback + `Authorization: Bearer <token>` 或 `X-Arachne-Helper-Token` |
| GET | `/capture/{job_id}` | 同上。返回状态，没有 Cookie |
| POST | `/capture/{job_id}/done` | 同上。保存当前浏览器里的 Cookie |

令牌来自 `ARACHNE_HOST_HELPER_TOKEN`。错误的令牌是 401。TCP 对端不是 loopback 是 403，即使令牌正确。来自非本机 Origin 的浏览器请求也是 403。查询字符串里的令牌无效。

状态：`starting`、`awaiting_user`、`saving`、`ready`、`failed`。`ready` 表示 `auto-<host>.bin` 已写入。

管理接口（仍受 `/admin` 的本机门禁约束，不进 OpenAPI）：

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | `/admin/api/capture/start` | 开始。助手可达时代理；在容器里则让浏览器去叫助手；在宿主机上且助手没开则本地打开浏览器 |
| GET | `/admin/api/capture/jobs/{job_id}` | 任务状态 |
| POST | `/admin/api/capture/jobs/{job_id}/done` | 完成并保存 |
| GET | `/admin/api/capture?url=` | 文件是否已出现。轮询用这个 |

只有容器需要浏览器转发时，`start` 的 JSON 里才有 `token`。页面 HTML 和文件轮询响应都不带令牌，也不回显 Cookie。

## 鉴权与本机限制

- 复用 `ARACHNE_REQUIRE_AUTH` / `ARACHNE_API_KEYS`，没有单独的 admin key。
- 关闭后台：`ARACHNE_ADMIN_ENABLED=false`（默认开启）。关闭后这些路径是 404。
- 默认只看 TCP 对端是不是 loopback（`127.0.0.0/8`、`::1`、IPv4-mapped loopback）。不看 `X-Forwarded-For`。
- 个人 Compose：`ARACHNE_ADMIN_TRUST_DOCKER=true`（默认 false）。此时对端可以是 Docker 网桥，但请求的 `Host` 必须是 `127.0.0.1` 或 `localhost`，可以带数字端口（`127.0.0.1:8000`）。其它 Host 仍然 403。仓库里的 `docker-compose.yml` 为个人使用设了这个变量。不要在共享网络上打开它；鉴权仍然走 `ARACHNE_API_KEYS`。

## 软墙 detail

顶层错误码仍是 `challenge_detected` / `unauthorized_upstream`。`detail` 额外有：

| 字段 | 含义 |
|------|------|
| `needs_session` | 软墙，管理页应给出建会话入口 |
| `host` | 去掉一个前导 `www.` 的主机名 |
| `auto_session_id` | 将写入的 `auto-<host>` id |
| `admin_hint` | 给人看的一句说明 |

成功响应的字段没有增加。

## 最近失败

进程内环形缓冲，最多 50 条，重启即丢。只记软墙，不记 Cookie。

## 助手没装好时

「开始」会说明要先跑 `python scripts/install_host_helper.py`。Cookie 粘贴仍然可用。

`scripts/capture_session.py` 仍可在宿主机上手动开浏览器（终端里按 Enter 保存）。这不是页面上的主路径。
