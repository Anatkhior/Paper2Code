"""解析语言是应用级偏好，不属于供应商配置，也不改变证据原文。"""

from typing import Literal

OutputLanguage = Literal["zh-CN", "en"]
DEFAULT_OUTPUT_LANGUAGE: OutputLanguage = "zh-CN"


def normalize_language(value: object) -> OutputLanguage:
    return "en" if value == "en" else DEFAULT_OUTPUT_LANGUAGE


def language_instruction(language: OutputLanguage) -> str:
    target = "简体中文（Simplified Chinese）" if language == "zh-CN" else "English（英语）"
    return (
        f"\n\n## 用户选择的解析语言\n所有面向用户的新解释必须使用 {target}。\n"
        "这适用于 name、one_liner、paper_summary、coverage_note、why、not_found_reason、confidence_reason，"
        "以及 explanation.intuition、math 中的解释文字、code_walkthrough 中每一条 text、pitfalls、read_next 和追问正文。"
        "不能只把概述写成目标语言，却把逐段代码讲解留成另一种语言。"
        "历史回答、论文和代码采用什么语言，都不改变此要求。"
        "但 paper_evidence.quote 必须逐字保留原文；文件路径、line_ref、函数/类/变量名、"
        "search_hints 中的检索标识符、公式符号、JSON 字段名、id 和状态枚举不翻译、不改写。"
    )


def localized_prompt(prompt: str, language: OutputLanguage) -> str:
    return prompt + language_instruction(language)
