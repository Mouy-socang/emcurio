"""采集共用异常。独立成模块：`python -m curio.curio_monitor` 时该模块是 __main__，
若 etsy.py 从它导入会得到第二个同名类，`except CollectionError` 就接不住 Etsy 的错误。"""


class CollectionError(Exception):
    """Sanitized error: response bodies, headers and credentials are never exposed."""
