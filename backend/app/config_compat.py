"""旧配置名的兼容边界；应用内部只使用 Paper2code 的规范名称。"""

from collections.abc import Mapping
from typing import Any

LEGACY_PREFIX = "PAPERLENS_"
PREFIX = "PAPER2CODE_"


def canonical_env(values: Mapping[str, Any]) -> dict[str, Any]:
    """同一来源内新名优先。支持系统环境的大写键和 pydantic 的小写键。"""
    result = {key: value for key, value in values.items() if not key.upper().startswith(LEGACY_PREFIX)}
    for key, value in values.items():
        if key.upper().startswith(LEGACY_PREFIX):
            prefix = PREFIX.lower() if key.islower() else PREFIX
            result.setdefault(prefix + key[len(LEGACY_PREFIX):], value)
    return result
