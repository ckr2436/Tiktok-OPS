from __future__ import annotations

from typing import Any

SUPPORTED_TASK_TYPES = {
    "general": "通用增长助手",
    "seo": "品牌 SEO 助手",
    "geo": "GEO / AI 搜索优化助手",
    "video_analysis": "短视频拆解助手",
    "script": "TikTok 短视频脚本助手",
    "product_copy": "产品页文案助手",
}

FEATURE_BY_TASK = {
    "general": "hermes_agent.use",
    "seo": "hermes_agent.seo",
    "geo": "hermes_agent.geo",
    "video_analysis": "hermes_agent.video_analysis",
    "script": "hermes_agent.script",
    "product_copy": "hermes_agent.product_copy",
}


def normalize_task_type(value: str | None) -> str:
    task_type = (value or "general").strip().lower().replace("-", "_")
    if task_type not in SUPPORTED_TASK_TYPES:
        return "general"
    return task_type


def feature_for_task(task_type: str) -> str:
    return FEATURE_BY_TASK.get(normalize_task_type(task_type), "hermes_agent.use")


def build_instructions(*, task_type: str, workspace_context: dict[str, Any] | None = None) -> str:
    task_type = normalize_task_type(task_type)
    context = workspace_context or {}
    brand = str(context.get("brand") or context.get("brand_name") or "MYUPONA").strip() or "MYUPONA"

    base = f"""
你是 GMV Ops 系统内置的跨境电商增长 Agent，服务品牌：{brand}。

你主要帮助运营团队完成：品牌 SEO、GEO/AI 搜索可见性、竞品短视频拆解、TikTok 短视频脚本、产品页文案和运营策略分析。

必须遵守：
1. 输出要具体、可执行、适合运营直接复制使用，不要只讲概念。
2. TikTok 脚本要像真实 UGC/种草内容，不要像硬广。
3. 对膳食补充剂、身体护理、个护产品，不得生成疾病治疗、治愈、诊断、药品化表达。
4. 避免绝对化承诺，例如 guaranteed、cure、heal、treat、pain relief cure 等。
5. 若内容涉及 MYUPONA 的睡眠软糖，优先使用 melatonin-free、wind down、relax before bed、wake refreshed 等合规方向。
6. 若内容涉及 MYUPONA 的身体护理膏，优先使用 cooling comfort、daily care、after workout、after long sitting、non-greasy、fast-absorbing 等合规方向。
7. 输出结构清楚，必要时用表格、分镜、脚本时间轴、清单。
""".strip()

    task_specific = {
        "general": "当前任务：作为通用增长助手，先理解用户给出的上下文，再输出可落地建议。",
        "seo": "当前任务：生成 SEO 标题、关键词、Meta Description、文章结构、FAQ、内部链接建议和合规提示。",
        "geo": "当前任务：优化品牌在 ChatGPT、Perplexity、Google AI Overview 等 AI 搜索场景中的可见性，输出实体描述、可引用问答、Schema 建议和内容资产规划。",
        "video_analysis": "当前任务：拆解短视频，包括 3 秒钩子、用户痛点、镜头节奏、字幕/口播、卖点承接、CTA、可复用脚本结构和改拍建议。",
        "script": "当前任务：生成 TikTok 短视频脚本，重点提升 3 秒停留率、完播率、评论互动和点击转化，输出时间轴、画面、口播、字幕、道具和 CTA。",
        "product_copy": "当前任务：生成产品页、卡片、包装、独立站模块文案，要求卖点清楚、合规、适合美国消费者。",
    }

    return f"{base}\n\n{task_specific[task_type]}"


def build_input_text(*, task_type: str, user_input: str | None, input_json: Any | None) -> str:
    task_type = normalize_task_type(task_type)
    parts = [f"任务类型：{SUPPORTED_TASK_TYPES[task_type]}"]
    if user_input and user_input.strip():
        parts.append("用户输入：\n" + user_input.strip())
    if input_json is not None:
        import json

        parts.append("结构化上下文 JSON：\n" + json.dumps(input_json, ensure_ascii=False, indent=2, default=str))
    return "\n\n".join(parts).strip()
