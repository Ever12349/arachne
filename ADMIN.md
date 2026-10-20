# Admin（0.11.0）

给人用的本机后台，挂在同一个 FastAPI 进程的 `/admin`。Agent 契约不变：仍然只要求 `POST /extract`。没有公开的 `/browse`。`/admin` 与 `/admin/api/*` 不进 OpenAPI，调用方不要依赖它们。

## 一键流程

1. 在本机打开 `http://127.0.0.1:8000/admin`（TCP 对端必须是 loopback）。
2. 软墙失败（`challenge_detected` 或 `unauthorized_upstream`）出现在仪表盘和「最近失败」里。URL 是链接。
3. 点 URL 或「为此建会话」。打开的「新建会话」已经填好这个 URL，并可以在新标签打开原页。
4. 复制页面上的命令，在**宿主机**执行（不要在容器里开有头浏览器）。先导出与服务相同的 `ARACHNE_SESSION_KEY` 和 `ARACHNE_SESSIONS_DIR`。有头浏览器打开后，人工通过验证码或登录墙，回到终端按 Enter。
5. 页面轮询 `auto-<host>.bin`。Compose 把 `./data` 挂进容器，所以宿主机写下的文件服务进程能看见。
6. 出现「重试抽取」后提交。同一条抽取管道会带上这个会话。

没有失败记录时，才在「新建会话」里手贴 URL，或贴扁平 Cookie JSON（`{"sid":"abc"}`），走和 `scripts/write_session.py` 一样的 Fernet 写入。

## 鉴权与本机限制

- 复用 `ARACHNE_REQUIRE_AUTH` / `ARACHNE_API_KEYS`，没有单独的 admin key。
- 关闭后台：`ARACHNE_ADMIN_ENABLED=false`（默认开启）。关闭后这些路径是 404。
- 只看 TCP 对端是不是 loopback（`127.0.0.0/8`、`::1`、IPv4-mapped loopback）。不信任 `Host` 或 `X-Forwarded-For`。
- `docker compose` 把端口发布到宿主机时，容器里看到的对端是网桥地址，不是 loopback，`/admin` 会 403。在宿主机跑 `uvicorn`，或在容器网络命名空间里访问 `127.0.0.1`。把发布地址写成 `127.0.0.1:8000:8000` 也不会改变容器内的对端地址。

## 采集命令

```bash
export ARACHNE_SESSION_KEY='…'
export ARACHNE_SESSIONS_DIR=./data/sessions
python scripts/capture_session.py --url 'https://example.com/article'
```

宿主机需要 Playwright（`requirements-playwright.txt` 与 `playwright install chromium`）。容器内的 Playwright 仍然是 headless，这个脚本不会在容器里启动。

命令和页面都不包含密钥，也不回显 Cookie。GET 只返回会话 id、是否 `auto-` 主机文件、mtime、采集命令和文件是否已出现。没有 Cookie 明文导出接口。

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
