"""Auto-tagging schema version 1.

Keep this module immutable after release. A prompt or vocabulary change must be
published as a new version so cached annotations remain reproducible.
"""

from __future__ import annotations

AUTO_TAGGING_SCHEMA_VERSION = 1
PROMPT_VERSION = "people-cosplay-v1"

CATEGORY_TAGS: dict[str, tuple[str, ...]] = {
    "content_domain": (
        "真人摄影",
        "Cosplay",
        "写真",
        "动漫插画",
        "二次元",
        "游戏角色",
        "舞台照",
    ),
    "people_count": ("无人物", "单人", "双人", "多人"),
    "shot_type": ("大特写", "特写", "半身", "七分身", "全身", "远景"),
    "actions": (
        "站立",
        "坐姿",
        "跪姿",
        "躺姿",
        "蹲姿",
        "行走",
        "奔跑",
        "跳跃",
        "回头",
        "挥手",
        "V手势",
        "托腮",
        "抱臂",
        "持物",
        "战斗姿势",
        "舞蹈",
        "伸展",
    ),
    "expressions": (
        "微笑",
        "大笑",
        "严肃",
        "平静",
        "惊讶",
        "害羞",
        "生气",
        "悲伤",
        "闭眼",
        "眨眼",
        "嘟嘴",
    ),
    "gaze": ("看镜头", "侧视", "向上看", "向下看", "背对镜头"),
    "appearance": (
        "黑发",
        "白发",
        "银发",
        "金发",
        "棕发",
        "红发",
        "蓝发",
        "紫发",
        "粉发",
        "绿发",
        "长发",
        "短发",
        "双马尾",
        "马尾",
        "卷发",
    ),
    "clothing": (
        "制服",
        "旗袍",
        "和服",
        "礼服",
        "女仆装",
        "泳装",
        "盔甲",
        "便装",
        "运动装",
        "连衣裙",
        "丝袜",
        "长筒袜",
        "头饰",
        "假发",
    ),
    "props": (
        "武器",
        "刀剑",
        "枪械",
        "法杖",
        "扇子",
        "雨伞",
        "手机",
        "乐器",
        "花束",
        "玩偶",
    ),
    "scene": (
        "室内",
        "户外",
        "摄影棚",
        "舞台",
        "城市",
        "自然",
        "海边",
        "森林",
        "雪景",
        "夜景",
        "校园",
    ),
    "photography": (
        "正面",
        "侧面",
        "背面",
        "仰拍",
        "俯拍",
        "低机位",
        "高机位",
        "逆光",
        "侧光",
        "柔光",
        "硬光",
        "浅景深",
        "黑白",
    ),
}

ENTITY_TYPES = ("real_person", "cosplayer", "character", "work")

_VOCABULARY_TEXT = "\n".join(
    f"- {category}: {', '.join(tags)}" for category, tags in CATEGORY_TAGS.items()
)

SYSTEM_PROMPT = f"""
你是本地人物、Cosplay、写真和二次元图库的视觉整理助手。请分析一张图片，
只输出一个合法 JSON 对象；不得输出 Markdown 代码块、解释或 JSON 之外的文字。

固定分类标签只能从以下词表原样选择：
{_VOCABULARY_TEXT}

角色名、作品名、真人姓名和 Cosplayer 名称不受固定词表限制，但必须遵守：
1. 人工标签优先于文件夹、文件名和 sidecar；这些显式元数据优先于画面推断。
2. 真人或 Cosplayer 身份不得仅凭面部或外观确认。只有人工标签、文件夹名、
   文件名、sidecar、可读 OCR 或水印等显式证据才可返回姓名；否则 name 必须为 null、
   state 必须为 unknown。
3. 角色和作品允许依据服装、道具和画面推断；证据不足时使用 suggested，冲突时
   使用 conflict，不确定时使用 unknown。
4. evidence 只能使用 manual_tag、folder_name、file_name、sidecar、ocr、watermark、
   visual 或 none。OCR/水印证据必须在 evidence_text 中引用看到的文字。
5. 不得把图片数量或文件大小作为标签，例如 120P、80张、30 images、1.2GB、850MB。
6. description 不超过 20 个字符。所有数组都必须存在，没有结果时返回空数组。

严格输出以下结构（schema_version 必须为 {AUTO_TAGGING_SCHEMA_VERSION}）：
{{
  "schema_version": {AUTO_TAGGING_SCHEMA_VERSION},
  "description": "不超过20个字符",
  "categories": {{
    "content_domain": [], "people_count": [], "shot_type": [],
    "actions": [], "expressions": [], "gaze": [], "appearance": [],
    "clothing": [], "props": [], "scene": [], "photography": []
  }},
  "entities": {{
    "real_person": [], "cosplayer": [], "character": [], "work": []
  }},
  "tags": [],
  "suggested_tags": []
}}

每个 entities 数组元素结构为：
{{"name": "名称或null", "state": "confirmed|suggested|conflict|unknown",
  "evidence": ["证据来源"], "evidence_text": "证据摘录", "confidence": 0.0}}
""".strip()
