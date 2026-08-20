"""People/Cosplay auto-tagging prompt revision 3.

Schema revision 2 remains the persisted wire contract.  Revision 3 tightens
the generation instructions and intentionally changes the prompt version so
durable failures produced by the older prompt are not reused.
"""

from __future__ import annotations

from .v2 import (
    AUTO_TAGGING_SCHEMA_VERSION,
    CATEGORY_TAGS,
    ENTITY_STATES,
    ENTITY_TYPES,
    FIELD_SPECS,
    FIELD_TAG_LABELS,
    FieldSpec,
)
from .v2 import (
    SYSTEM_PROMPT as _V2_SYSTEM_PROMPT,
)

PROMPT_VERSION = "people-cosplay-v3"

__all__ = [
    "AUTO_TAGGING_SCHEMA_VERSION",
    "CATEGORY_TAGS",
    "ENTITY_STATES",
    "ENTITY_TYPES",
    "FIELD_SPECS",
    "FIELD_TAG_LABELS",
    "FieldSpec",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
]

SYSTEM_PROMPT = (
    _V2_SYSTEM_PROMPT
    + """

补充约束（提示词修订 v3）：
1. description 最多 20 个 Unicode 字符，输出前必须自行截断。
2. values 只能使用字段清单中逐字存在的机器码；不允许创造近义码。
3. 每个字段必须是 {"values": [...], "confidence": 0.0} 形式，禁止只返回数组。
4. entities 的每一项必须是对象；evidence 必须是数组。
5. unable_to_confirm 或 not_applicable 状态下 name 必须为 null。
""".strip()
)


def _validate_assets() -> None:
    if AUTO_TAGGING_SCHEMA_VERSION != 2:
        raise RuntimeError("Prompt v3 must keep the released schema-v2 contract.")
    if PROMPT_VERSION != "people-cosplay-v3":
        raise RuntimeError("Prompt v3 version constant is invalid.")
    for required_text in (
        "description 最多 20",
        '"values"',
        '"confidence"',
        "unable_to_confirm",
    ):
        if required_text not in SYSTEM_PROMPT:
            raise RuntimeError(f"Prompt v3 is missing required text: {required_text}")


_validate_assets()
