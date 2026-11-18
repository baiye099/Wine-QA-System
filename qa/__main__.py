"""命令行启动文件。使用 python -m qa。"""

import sys

from .router.cli import main


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
