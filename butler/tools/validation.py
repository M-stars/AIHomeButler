"""JSON Schema 子集校验（阶段 1 够用：string/number/integer/boolean/array/object
+ required/enum/minimum/maximum/additionalProperties）。

阶段 4 如需更完整校验能力，优先复用 jsonschema 库，本模块即替换点。
"""
from __future__ import annotations

from typing import Any, Dict, List

SUPPORTED_TYPES: Dict[str, tuple] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_arguments(args: Any, schema: Dict) -> List[str]:
    """返回中文错误列表；空列表表示通过。"""
    if not isinstance(args, dict):
        return ["参数必须是 JSON 对象"]
    errors: List[str] = []
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in args:
            errors.append(f"缺少必填参数 {name}")
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in properties:
                errors.append(f"不接受未知参数 {key}")
    for name, value in args.items():
        spec = properties.get(name)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if expected in SUPPORTED_TYPES:
            pytype = SUPPORTED_TYPES[expected]
            if isinstance(value, bool) and expected != "boolean":
                errors.append(f"参数 {name} 类型应为 {expected}，实际为 boolean")
            elif not isinstance(value, pytype):
                actual = type(value).__name__
                errors.append(f"参数 {name} 类型应为 {expected}，实际为 {actual}")
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"参数 {name} 取值 {value!r} 不在允许范围 {spec['enum']} 内")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                errors.append(f"参数 {name} 不能小于 {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                errors.append(f"参数 {name} 不能大于 {spec['maximum']}")
    return errors
