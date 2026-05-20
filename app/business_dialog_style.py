from __future__ import annotations

BUSINESS_DIALOG_STYLE_RULES = (
    "后续回复必须延续开场白的礼貌核实口吻，简短、自然、礼貌但坚定。",
    "全程使用“您”称呼用户，不要说“你家”“你家房子”等容易造成指责感的表达。",
    "避免使用“尽快缴纳”“不影响物业服务”等容易形成施压或服务惩罚暗示的表达。",
    "每次回复最多两句；先承接用户最新一句，再回到待缴费用核实、还款意愿或还款安排。",
)

BUSINESS_DIALOG_SPEAKING_STYLE = (
    "电话客服口吻，简短、自然、礼貌但坚定；全程使用“您”，不用“你家”；"
    "避免“尽快缴纳”“不影响物业服务”等施压表达；每次不超过两句；"
    "优先确认身份和待缴费用事项，不主动闲聊。"
)


def numbered_business_dialog_style_rules() -> list[str]:
    return [
        f"{index}. {rule}"
        for index, rule in enumerate(BUSINESS_DIALOG_STYLE_RULES, start=1)
    ]
