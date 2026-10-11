"""
部署入口脚本。

宝塔里：
    框架     = python  （或 Flask —— Flask 是 WSGI，gunicorn 也能跑）
    启动方式 = python
    运行文件 = run.py

本脚本同时把 app 暴露在模块层，方便以后切到 gunicorn：
    gunicorn -w 2 -b 0.0.0.0:8000 run:app
"""
from typecho_agent_api.server import app, main  # 暴露给 gunicorn run:app 用

__all__ = ["app"]


if __name__ == "__main__":
    # 直接用 Flask 自带服务器只适合本地调试：它没有进程守护，崩了不会自己起来。
    # 产线请用：  gunicorn -w 2 -k gthread --threads 4 -b 0.0.0.0:8000 run:app
    main()
