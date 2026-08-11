from __future__ import annotations

from typing import Any

SUPPORTED_TASK_TYPES = {
    "general": "通用增长助手",
    "seo": "品牌 SEO 助手",
    "geo": "GEO / AI 搜索优化助手",
    "video_analysis": "短视频拆解助手",
    "script": "短视频脚本助手",
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
    brand = str(context.get("brand") or context.get("brand_name") or "").strip()
    brand_label = brand or "当前工作区品牌"
    channel = str(
        context.get("channel")
        or context.get("platform")
        or "短视频社交平台"
    ).strip()
    approved_claims = str(context.get("approved_claims") or "").strip()
    prohibited_claims = str(context.get("prohibited_claims") or "").strip()

    base = f"""
你是 GMV Ops 系统内置的跨境电商增长 Agent，服务品牌：{brand_label}。

你主要帮助运营团队完成：品牌 SEO、GEO/AI 搜索可见性、竞品短视频拆解、{channel}短视频脚本、产品页文案和运营策略分析。

必须遵守：
1. 输出要具体、可执行、适合运营直接复制使用，不要只讲概念。
2. 短视频脚本要符合项目配置的平台与受众语境，不要擅自假设渠道、价格、购物入口或品牌事实。
3. 对膳食补充剂、身体护理、个护产品，不得生成疾病治疗、治愈、诊断、药品化表达。
4. 避免绝对化承诺，例如 guaranteed、cure、heal、treat、pain relief cure 等。
5. 只使用工作区显式提供或产品资料确认的品牌、卖点、价格、成分、包装和 CTA，不得补写默认产品知识。
6. 输出结构清楚，必要时用表格、分镜、脚本时间轴、清单。
""".strip()
    if approved_claims:
        base += f"\n7. 已确认可用事实：{approved_claims}"
    if prohibited_claims:
        base += f"\n8. 明确禁用表达：{prohibited_claims}"

    task_specific = {
        "general": "当前任务：作为通用增长助手，先理解用户给出的上下文，再输出可落地建议。",
        "seo": "当前任务：生成 SEO 标题、关键词、Meta Description、文章结构、FAQ、内部链接建议和合规提示。",
        "geo": "当前任务：优化品牌在 ChatGPT、Perplexity、Google AI Overview 等 AI 搜索场景中的可见性，输出实体描述、可引用问答、Schema 建议和内容资产规划。",
        "video_analysis": "当前任务：拆解短视频，包括 3 秒钩子、用户痛点、镜头节奏、字幕/口播、卖点承接、CTA、可复用脚本结构和改拍建议。",
        "script": f"当前任务：生成适合{channel}的短视频脚本，重点提升开场停留、完播、互动和点击转化，输出时间轴、画面、口播、字幕、道具和项目授权的 CTA。",
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
