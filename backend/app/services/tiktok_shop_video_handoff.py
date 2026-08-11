from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any, Mapping

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.data.models.hermes_agent import HermesContentProducerAttachment
from app.data.models.tiktok_shop import TikTokShopVideoContentAnalysis
from app.services.hermes_agent.content_producer import (
    get_or_create_producer_conversation,
    producer_attachment_out,
    save_producer_attachment,
)
from app.services.tiktok_shop_video_analysis import serialize_analysis


HANDOFF_VERSION = "shop-video-optimization-v1"


def _text(value: Any, fallback: str = "未提供") -> str:
    normalized = "" if value is None else str(value).strip()
    return normalized if normalized else fallback


def _number(value: Any) -> str:
    if value is None:
        return "未观测到"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return f"{value:,}"
    try:
        return f"{float(value):,.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _text(value)


def _money(value: Any, currency: Any) -> str:
    return "未观测到" if value is None else f"{_text(currency, 'USD')} {_number(value)}"


def _percent(value: Any) -> str:
    if value is None:
        return "未观测到"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return _text(value)


def _lines(values: Any, *, ordered: bool = False) -> list[str]:
    if not isinstance(values, list) or not values:
        return ["- 未提供"]
    output: list[str] = []
    for index, raw in enumerate(values, 1):
        if isinstance(raw, Mapping):
            content = "；".join(
                f"{key}: {_text(value)}"
                for key, value in raw.items()
                if value not in (None, "", [], {})
            )
        else:
            content = _text(raw)
        output.append(f"{index}. {content}" if ordered else f"- {content}")
    return output


def _facts(title: str, value: Any) -> list[str]:
    lines = [f"### {title}"]
    if isinstance(value, Mapping) and value:
        lines.extend(
            f"- {key}: {_text(item)}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        )
    else:
        lines.append("- 未提供")
    return lines


def render_video_analysis_report(
    row: TikTokShopVideoContentAnalysis,
    *,
    report_format: str = "markdown",
) -> str:
    """Render the persisted evidence packet without recomputing live metrics."""

    payload = serialize_analysis(row)
    if report_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if report_format != "markdown":
        raise APIError("VIDEO_ANALYSIS_REPORT_FORMAT_INVALID", "Use markdown or json.", 400)

    video = dict(payload.get("video") or {})
    shop = dict((payload.get("metrics") or {}).get("shop") or {})
    paid = dict((payload.get("metrics") or {}).get("paid") or {})
    transcript = dict(payload.get("transcript") or {})
    analysis = dict(payload.get("analysis") or {})
    currency = shop.get("currency") or "USD"
    title = re.sub(r"[\r\n]+", " ", _text(video.get("title"), f"视频 {row.video_id}"))[:500]
    products = video.get("products") if isinstance(video.get("products"), list) else []
    product_lines = [
        f"- {_text(item.get('name'), '未命名商品')} (商品 ID: {_text(item.get('product_id'))})"
        for item in products
        if isinstance(item, Mapping)
    ] or ["- 官方视频记录未返回关联商品"]

    lines = [
        f"# TikTok Shop 视频运营分析报告：{title}",
        "",
        "## 报告范围",
        f"- 视频 ID: {row.video_id}",
        f"- 发布时间: {_text(video.get('posted_at'))}",
        f"- 创作者: {_text(video.get('creator_username'))}",
        f"- 统计开始日: {payload['metric_start_date']}",
        f"- 统计结束日（不含）: {payload['metric_end_date_exclusive']}",
        f"- 分析模型角色: {_text(payload.get('model'))}",
        f"- 提示词版本: {_text(payload.get('prompt_version'))}",
        f"- 分析完成时间: {_text(payload.get('completed_at'))}",
        "",
        "## 数据边界",
        "- Shop 指标来自 TikTok Shop Analytics 视频日级官方数据。",
        "- GMV Max 指标来自 TikTok Business 创意日级官方数据；未匹配到表示未观测到，不按 0 处理。",
        "- 关联商品只表达内容关系，不把视频 GMV 强制分摊给单个商品。",
        "- 本报告冻结生成时的持久化证据，不在导出时重新查询或改写历史指标。",
        "",
        "## 关联商品",
        *product_lines,
        "",
        "## Shop 内容经营（官方）",
        f"- 播放量: {_number(shop.get('views'))}",
        f"- 视频 GMV: {_money(shop.get('gmv'), currency)}",
        f"- 千次播放产出: {_money(shop.get('gpm'), currency)}",
        f"- 商品点击率: {_percent(shop.get('click_through_rate'))}",
        f"- SKU 订单: {_number(shop.get('sku_orders'))}",
        f"- 售出件数: {_number(shop.get('items_sold'))}",
        f"- 覆盖统计日: {_number(shop.get('days_present'))}",
        f"- 最新官方数据日: {_text(shop.get('latest_report_date'))}",
        "",
        "## GMV Max 投放（官方）",
    ]
    if paid.get("available"):
        lines.extend([
            f"- 商品广告曝光: {_number(paid.get('product_impressions'))}",
            f"- 商品广告点击: {_number(paid.get('product_clicks'))}",
            f"- 消耗: {_money(paid.get('cost'), currency)}",
            f"- 广告成交: {_money(paid.get('gross_revenue'), currency)}",
            f"- 订单: {_number(paid.get('orders'))}",
            f"- ROI: {_number(paid.get('roi'))}",
            f"- 转化率: {_percent(paid.get('conversion_rate'))}",
            f"- 2 秒播放率: {_percent(paid.get('view_rate_2s'))}",
            f"- 6 秒播放率: {_percent(paid.get('view_rate_6s'))}",
            f"- 25% / 50% / 75% / 完播: {_percent(paid.get('view_rate_25'))} / {_percent(paid.get('view_rate_50'))} / {_percent(paid.get('view_rate_75'))} / {_percent(paid.get('view_rate_100'))}",
            f"- 质量标记: {_text('、'.join(paid.get('rate_quality_flags') or []), '无')}",
        ])
    else:
        lines.append("- 所选周期未匹配到该视频的 GMV Max 创意指标。")

    lines.extend([
        "",
        "## 口播与字幕",
        f"- 状态: {_text(transcript.get('status'))}",
        f"- 语言: {_text(transcript.get('language'))}",
        "",
        _text(transcript.get("text"), "未提取到可靠口播文本。"),
        "",
        "## Hermes 结论",
        _text(analysis.get("summary")),
        "",
        f"- 置信度: {_percent(analysis.get('confidence'))}",
        "",
    ])
    for section_title, key in (
        ("内容定位", "content_profile"),
        ("前 2 秒钩子", "hook_analysis"),
        ("商品露出与证明", "product_analysis"),
        ("口播文案结构", "spoken_copy_analysis"),
        ("节奏与掉点", "pacing_analysis"),
        ("行动号召", "cta_analysis"),
    ):
        lines.extend(_facts(section_title, analysis.get(key)))
        lines.append("")

    lines.extend(["## 逐段内容拆解", *_lines(analysis.get("timeline")), ""])
    lines.extend(["## 有效做法", *_lines(analysis.get("strengths")), ""])
    lines.extend(["## 主要问题与证据", *_lines(analysis.get("problems")), ""])
    lines.extend(["## 下一步动作", *_lines(analysis.get("actions"), ordered=True), ""])
    lines.extend(_facts("下一轮单变量实验", analysis.get("next_experiment")))
    lines.extend(["", "## 证据限制", *_lines(analysis.get("limitations")), ""])
    return "\n".join(lines).rstrip() + "\n"


def video_analysis_report_filename(
    row: TikTokShopVideoContentAnalysis,
    *,
    report_format: str,
) -> str:
    extension = "json" if report_format == "json" else "md"
    return f"tiktok-video-analysis-{row.video_id}-{row.id}.{extension}"


def build_optimization_draft(row: TikTokShopVideoContentAnalysis) -> str:
    analysis = dict(row.analysis_json or {})
    video = dict((row.input_summary_json or {}).get("video") or {})
    summary = _text(analysis.get("summary"))
    experiment = analysis.get("next_experiment")
    experiment_text = (
        "；".join(f"{key}: {_text(value)}" for key, value in experiment.items())
        if isinstance(experiment, Mapping)
        else _text(experiment)
    )
    return (
        f"请基于已导入的视频分析报告和参考视频，优化视频《{_text(video.get('title'), row.video_id)}》。\n\n"
        f"当前核心结论：{summary}\n"
        f"建议单变量实验：{experiment_text}\n\n"
        "请先给出一份可确认的优化方案，明确：1）必须保留的有效元素；2）按证据优先修复的问题；"
        "3）仅改变一个变量的下一版实验；4）目标指标与观察周期。不要虚构产品功效、价格、优惠或用户评价。"
        "关联商品信息仅作线索，请让我从内容工厂商品库选择权威商品后，再生成制作任务。"
    )


def _source_attachment(
    rows: list[HermesContentProducerAttachment],
    *,
    analysis_id: int,
    source_kind: str,
) -> HermesContentProducerAttachment | None:
    return next(
        (
            item for item in rows
            if int(dict(item.meta_json or {}).get("source_analysis_id") or 0) == int(analysis_id)
            and dict(item.meta_json or {}).get("source_kind") == source_kind
        ),
        None,
    )


def _discard_created_attachments(
    db: Session,
    rows: list[HermesContentProducerAttachment],
) -> None:
    for row in rows:
        for raw_path in (row.file_path, row.preview_path):
            if raw_path:
                Path(raw_path).unlink(missing_ok=True)
        db.delete(row)


async def create_content_factory_video_handoff(
    db: Session,
    *,
    row: TikTokShopVideoContentAnalysis,
    user_id: int,
    media: tuple[Path, str] | None,
) -> dict[str, Any]:
    if str(row.status).upper() != "SUCCEEDED" or not isinstance(row.analysis_json, dict):
        raise APIError(
            "VIDEO_ANALYSIS_NOT_READY",
            "Complete the video analysis before sending it to Content Factory.",
            409,
        )
    session_key = f"video-opt-{int(row.id)}-{str(row.cache_key)[:12]}"[:48]
    conversation = get_or_create_producer_conversation(
        db,
        workspace_id=int(row.workspace_id),
        user_id=int(user_id),
        session_key=session_key,
    )
    # Serialize repeated clicks for this user/session before checking staged files.
    conversation = db.scalar(
        select(type(conversation)).where(type(conversation).id == int(conversation.id)).with_for_update()
    ) or conversation
    attachments = list(
        db.scalars(
            select(HermesContentProducerAttachment)
            .where(
                HermesContentProducerAttachment.conversation_id == int(conversation.id),
                HermesContentProducerAttachment.workspace_id == int(row.workspace_id),
                HermesContentProducerAttachment.user_id == int(user_id),
            )
            .order_by(HermesContentProducerAttachment.id.asc())
        ).all()
    )
    created: list[HermesContentProducerAttachment] = []

    report_attachment = _source_attachment(
        attachments, analysis_id=int(row.id), source_kind="analysis_report"
    )
    if report_attachment is None:
        report = render_video_analysis_report(row).encode("utf-8")
        upload = UploadFile(
            file=BytesIO(report),
            filename=video_analysis_report_filename(row, report_format="markdown"),
            size=len(report),
            headers={"content-type": "text/markdown; charset=utf-8"},
        )
        report_attachment = await save_producer_attachment(
            db,
            conversation=conversation,
            user_id=int(user_id),
            upload=upload,
            kind="brief_document",
        )
        report_attachment.meta_json = {
            **dict(report_attachment.meta_json or {}),
            "source_analysis_id": int(row.id),
            "source_video_id": str(row.video_id),
            "source_kind": "analysis_report",
            "handoff_version": HANDOFF_VERSION,
        }
        db.add(report_attachment)
        created.append(report_attachment)
        attachments.append(report_attachment)

    video_attachment = _source_attachment(
        attachments, analysis_id=int(row.id), source_kind="reference_video"
    )
    media_unavailable = media is None
    try:
        if video_attachment is None and media is not None:
            media_path, content_type = media
            upload = UploadFile(
                file=media_path.open("rb"),
                filename=f"tiktok-video-{row.video_id}{media_path.suffix or '.mp4'}",
                size=media_path.stat().st_size,
                headers={"content-type": content_type or "video/mp4"},
            )
            try:
                video_attachment = await save_producer_attachment(
                    db,
                    conversation=conversation,
                    user_id=int(user_id),
                    upload=upload,
                    kind="reference_video",
                )
            finally:
                await upload.close()
            transcript = serialize_analysis(row).get("transcript") or {}
            transcript_status = str(transcript.get("status") or "UNAVAILABLE").lower()
            transcript_segments = list(transcript.get("segments") or [])
            transcript_text = str(transcript.get("text") or "").strip()
            if transcript_status in {"ready", "success", "succeeded", "completed"}:
                transcript_status = (
                    "success" if transcript_text or transcript_segments else "no_speech"
                )
            video_attachment.analysis_status = "processing"
            video_attachment.analysis_json = {
                **dict(video_attachment.analysis_json or {}),
                "transcript_status": transcript_status,
                "transcript_language": transcript.get("language"),
                "transcript_text": transcript_text,
                "transcript_segments": transcript_segments,
                # Keep the canonical fields consumed by the benchmark worker.
                # The source analysis already owns a completed transcript, so
                # Content Factory must not spend another Whisper pass on it.
                "detected_language": transcript.get("language"),
                "transcript": transcript_text,
                "segments": transcript_segments,
                "multimodal_status": "queued",
                "source_analysis_id": int(row.id),
                "analysis_reused": True,
            }
            video_attachment.meta_json = {
                **dict(video_attachment.meta_json or {}),
                "source_analysis_id": int(row.id),
                "source_video_id": str(row.video_id),
                "source_kind": "reference_video",
                "handoff_version": HANDOFF_VERSION,
            }
            db.add(video_attachment)
            created.append(video_attachment)
            attachments.append(video_attachment)
    except Exception:
        _discard_created_attachments(db, created)
        raise

    draft = build_optimization_draft(row)
    video = dict((row.input_summary_json or {}).get("video") or {})
    meta = dict(conversation.meta_json or {})
    meta.update({
        "session_key": session_key,
        "status": "idle",
        "source_type": "tiktok_shop_video_analysis",
        "source_analysis_id": int(row.id),
        "source_video_id": str(row.video_id),
        "source_cache_key": str(row.cache_key),
        "handoff_version": HANDOFF_VERSION,
        "draft_message": draft,
        "source_title": _text(video.get("title"), str(row.video_id))[:500],
    })
    conversation.title = f"视频优化 · {_text(video.get('title'), str(row.video_id))}"[:255]
    conversation.meta_json = meta
    db.add(conversation)
    db.flush()
    pending_multimodal_attachment_ids = [
        int(item.id)
        for item in attachments
        if item.kind == "reference_video"
        and str(dict(item.analysis_json or {}).get("multimodal_status") or "")
        == "queued"
    ]
    return {
        "session_key": session_key,
        "content_factory_url": (
            f"/tenants/{int(row.workspace_id)}/hermes-agent/content-factory"
            f"?producer_session={session_key}&source=video-analysis"
        ),
        "draft_message": draft,
        "attachments": [producer_attachment_out(item) for item in attachments],
        "report_attached": report_attachment is not None,
        "video_attached": video_attachment is not None,
        "media_unavailable": media_unavailable,
        "created_attachment_keys": [item.attachment_key for item in created],
        "pending_multimodal_attachment_ids": pending_multimodal_attachment_ids,
        "reused": not created,
    }
