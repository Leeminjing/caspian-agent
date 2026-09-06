"""
本文件对外提供 fenced/纯 JSON 文本解析助手，供 judge 与 rating 等模块复用。

对外提供:
    parse_fenced_or_raw — 从模型文本中提取 JSON 对象（fenced 优先，其次整段解析）

输入:
    text: str — 模型输出文本

输出:
    dict | None — 解析出的 JSON 对象；无法解析时抛 json.JSONDecodeError（由调用方处理）

示例:
    parse_fenced_or_raw('```json\n{"conflicts": []}\n```')  # → {"conflicts": []}
    parse_fenced_or_raw('{"a": 1}')                          # → {"a": 1}
"""

import json
import re

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def parse_fenced_or_raw(text: str) -> dict | None:
    """从模型文本中提取 JSON 对象（fenced 优先，其次整段解析）。"""
    fenced = _FENCED_JSON_RE.search(text)
    if fenced:
        return json.loads(fenced.group(1))
    return json.loads(text)
