"""pytest 公共配置：把项目根（含 weft 包的目录）加入 sys.path。"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
