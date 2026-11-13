"""从仓库源码直接启动，无需安装为发行版。"""

from src.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
