import os
import sys

_HERE = os.path.dirname(__file__)
for _sub in ("web_search_lambda", "research_agent"):
    _path = os.path.join(_HERE, _sub)
    if _path not in sys.path:
        sys.path.insert(0, _path)
