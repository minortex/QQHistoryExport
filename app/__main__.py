"""命令行入口：uv run qqhe / python -m app serve"""

from __future__ import annotations

import argparse
import threading
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="qqhe",
        description="QQ 聊天记录导出 Web 服务",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve"],
        help="默认 serve",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()

    if args.open:
        threading.Timer(
            1.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}/")
        ).start()

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
