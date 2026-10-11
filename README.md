# Typecho Agent API

Flask + 腾讯云 COS，给 LLM Agent 用的 Typecho 博客控制平面 —— 文章 CRUD + 图片上传。

部署在 Typecho 服务器上，agent 配 `TYPECHO_API_BASE_URL` + `TYPECHO_API_KEY` 两个环境变量按 [skills/typecho-blog/SKILL.md](skills/typecho-blog/SKILL.md) 调用即可。

---

## 安装与启动

```bash
# 1) 装包
pip install -r requirements.txt

# 2) 配 .env（从 .env.example 复制后改）
cp .env.example .env
vim .env   # 必改：TYPECHO_DB_* / COS_* / API_KEYS

# 3) 启动（仅本地调试）
python run.py
# → Flask 自带开发服务器，输出会显示:
#   * Running on http://0.0.0.0:8000
```

### 生产部署

**不要用 `python run.py` 跑生产。** Flask 自带服务器没有守护、没有超时、没有 worker 管理，
进程崩了不会自己起来，这也是「服务跑着跑着就挂了」最常见的直接原因。用 gunicorn：

```bash
pip install gunicorn
gunicorn -w 2 -k gthread --threads 4 \
         --timeout 30 --graceful-timeout 30 \
         -b 127.0.0.1:8000 run:app
```

再交给进程管理器兜底（下面两个选一个）：

```ini
# supervisor:/etc/supervisor/conf.d/typecho-api.conf
[program:typecho-api]
command=/path/to/venv/bin/gunicorn -w 2 -k gthread --threads 4 --timeout 30 -b 127.0.0.1:8000 run:app
directory=/path/to/typecho-server-and-skill
autostart=true
autorestart=true          ; ← 挂了自动拉起
startretries=3
stopasgroup=true
stdout_logfile=/var/log/typecho-api.out.log
stderr_logfile=/var/log/typecho-api.err.log
```

```ini
# systemd:/etc/systemd/system/typecho-api.service
[Service]
WorkingDirectory=/path/to/typecho-server-and-skill
ExecStart=/path/to/venv/bin/gunicorn -w 2 -k gthread --threads 4 --timeout 30 -b 127.0.0.1:8000 run:app
Restart=always            ; ← 挂了自动拉起
RestartSec=3
[Install]
WantedBy=multi-user.target
```

对外只暴露 443，用 Nginx 反代到 `127.0.0.1:8000`（服务本身不要直接监听 0.0.0.0）。

宝塔部署：Python 项目 → 启动方式选 **gunicorn**，入口 `run:app`，端口用 `.env` 里的 `HTTP_PORT`（默认 8000）；
并确认开启「进程守护 / 崩溃自动重启」。详见下面的 [生产部署](#生产部署)。

---

## API 一览（全部要求 `X-API-Key` 头）

| Method | Path | 说明 |
|---|---|---|
| `GET`  | `/healthz` | 健康检查（**免鉴权**） |
| `GET`  | `/v1/tools` | 列出 14 个工具 schema |
| `POST` | `/v1/tools/call` | 调用任意工具 `{name, arguments}` |
| `POST` | `/v1/tools/call_openai` | 转发 OpenAI 风格的 `tool_calls` |
| `POST` | `/v1/posts` | 新增文章 |
| `GET`  | `/v1/posts/{cid}` | 获取文章 |
| `PATCH` | `/v1/posts/{cid}` | 局部更新 |
| `DELETE` | `/v1/posts/{cid}` | 删除文章 |
| `GET`  | `/v1/posts?page=1&page_size=20` | 列表，支持 `status` / `keyword` |
| `GET`  | `/v1/posts/by-slug/{slug}` | 按 slug 查找 |
| `POST` | `/v1/images/upload` | multipart 上传图片 |
| `POST` | `/v1/images/upload_base64` | base64 上传图片 |
| `DELETE` | `/v1/images/{key:path}` | 删除图片 |

`POST /v1/tools/call` 业务错误**不会**抛 4xx，返回 `{"ok": false, "error": "..."}`。

---

## 项目结构

```
typecho-agent-api/
├── README.md
├── LICENSE
├── pyproject.toml
├── requirements.txt
├── run.py                 # 部署入口（python run.py / gunicorn run:app）
├── .env.example
├── src/typecho_agent_api/ # Python package
│   ├── __init__.py
│   ├── config.py          # .env 加载
│   ├── auth.py            # X-API-Key 校验
│   ├── typecho_db.py      # MySQL 池
│   ├── metas.py           # 分类/标签
│   ├── contents.py        # 文章 CRUD
│   ├── cos_uploader.py    # 腾讯云 COS
│   ├── agent_tools.py     # 14 个 tool + dispatcher
│   └── server.py          # Flask app
└── skills/
    └── typecho-blog/
        └── SKILL.md       # 给 agent 用的说明书（HTTP + API key）
```

---

## 给 agent 用

agent 端**不需要装任何东西**，加载 [skills/typecho-blog/SKILL.md](skills/typecho-blog/SKILL.md) + 设 2 个环境变量就行：

```bash
export TYPECHO_API_BASE_URL="https://blog.example.com"
export TYPECHO_API_KEY="sk-xxxxxx"
```

SKILL.md 里含 14 个工具的完整 JSON schema、curl / Python / OpenAI 三套调用示例、字段约定、错误处理。

---

## License

MIT
