"""華為雲 MaaS 大模型配置（公開常量；API Key 在 config/settings.py）。

控制台：西南-貴陽一 cn-southwest-2 → MaaS → 在線推理 → 預置服務。
2026-11-11 實測：
- 三個模型皆接受 OpenAI 兼容的 messages / stream / tools / response_format。
- DeepSeek 與 openPangu 預設開啟思考，可用 thinking.type=disabled 關閉。
- GLM-5.3 傳 thinking.type=disabled 會返回 400，只能保持思考開啟。
"""

from __future__ import annotations

from dataclasses import dataclass

MAAS_PROVIDER = "華為雲 MaaS"
MAAS_REGION = "cn-southwest-2"
MAAS_CHAT_URL = "https://api.modelarts-maas.com/v2/chat/completions"
MAAS_MODELS_VERIFIED_ON = "2026-11-11"
MAAS_DOCS_URL = "https://support.huaweicloud.com/model-call-maas/model-call-005.html"


@dataclass(frozen=True)
class ChatModel:
    id: str                     # 前端與請求中使用的模型 ID（即 MaaS model 參數）
    name: str                   # 對外顯示的完整版本名
    tag: str                    # 「⋯」選單裡名稱旁的括號說明
    hint: str                   # 「⋯」選單裡的灰色補充說明
    can_disable_thinking: bool  # GLM-5.3 不支援關閉思考

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "tag": self.tag, "hint": self.hint,
                "always_thinks": not self.can_disable_thinking}


DEEPSEEK_V41_FLASH = ChatModel(
    id="deepseek-v4.1-flash-1017", name="DeepSeek-V4.1-Flash",
    tag="預設", hint="解題、推理均衡", can_disable_thinking=True,
)
GLM_53 = ChatModel(
    id="glm-5.3", name="GLM-5.3",
    tag="高級專家模式", hint="會先深度思考，回覆較慢", can_disable_thinking=False,
)
OPENPANGU_20_FLASH = ChatModel(
    id="openpangu-2.0-flash", name="openPangu-2.0-Flash",
    tag="快速處理／文字處理", hint="回覆快，適合整理文字", can_disable_thinking=True,
)

# 「⋯」選單中的顯示順序
CHAT_MODELS: dict[str, ChatModel] = {
    model.id: model for model in (DEEPSEEK_V41_FLASH, GLM_53, OPENPANGU_20_FLASH)
}
DEFAULT_CHAT_MODEL = DEEPSEEK_V41_FLASH.id
DEFAULT_VISION_MODEL = DEEPSEEK_V41_FLASH.id
VISION_MODELS = frozenset({DEFAULT_VISION_MODEL})
# 出題、上傳分類、記憶提取、文件助手等後台任務固定使用預設模型，不跟隨用戶選擇。
BACKGROUND_MODEL = DEFAULT_CHAT_MODEL


def resolve_chat_model(model_id: str | None) -> ChatModel:
    """未知或空值一律回落到預設模型。"""
    return CHAT_MODELS.get(model_id or "", CHAT_MODELS[DEFAULT_CHAT_MODEL])


def llm_request_options(*, think: bool = False, model: str | None = None) -> dict:
    """返回 model 與思考開關；thinking 參數在 MaaS 必須按模型能力傳遞。"""
    selected = resolve_chat_model(model or BACKGROUND_MODEL)
    if not selected.can_disable_thinking:
        return {"model": selected.id, "thinking": {"type": "enabled"}}
    options = {"model": selected.id, "thinking": {"type": "enabled" if think else "disabled"}}
    if think:
        options["reasoning_effort"] = "high"
    return options


def public_chat_models() -> list[dict]:
    return [{**model.public(), "supports_images": model.id in VISION_MODELS} for model in CHAT_MODELS.values()]
