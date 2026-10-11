"""
gunicorn 配置。用于宝塔「启动方式 = 命令行」或手动启动：

    gunicorn -c gunicorn.conf.py run:app

命令行参数优先级高于本文件，所以也可以用 -b 覆盖监听地址：
    gunicorn -c gunicorn.conf.py -b 127.0.0.1:9000 run:app
"""

# 只监听本机，外网一律走 Nginx 反向代理，不要把 8000 直接暴露到公网
bind = "127.0.0.1:8000"

# 项目是同步的 Flask + PyMySQL 代码，用 gthread（多进程 + 每进程线程池）
# 比纯多进程省内存，也够用
workers = 2
worker_class = "gthread"
threads = 4

# 关键：单个请求的超时上限。没有它，一个卡住的请求能把这个 worker 永久占住，
# worker 全被占满后服务就「假死」了
timeout = 30
graceful_timeout = 30

# 定期回收 worker，避免长时间运行后内存缓慢增长
max_requests = 1000
max_requests_jitter = 100

# 不能在 master 里预加载 app：那样连接池会在 fork 之前建好，
# 多个 worker 共享同一批 MySQL 连接会互相踩踏
preload_app = False

# 日志输出到 stdout/stderr，交给进程管理器（宝塔 / supervisor / systemd）收集
accesslog = "-"
errorlog = "-"
loglevel = "info"
