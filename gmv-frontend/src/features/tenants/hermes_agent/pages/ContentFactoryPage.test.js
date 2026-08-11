import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const source = readFileSync(
  join(process.cwd(), 'src/features/tenants/hermes_agent/pages/ContentFactoryPage.jsx'),
  'utf8',
)
const apiSource = readFileSync(
  join(process.cwd(), 'src/features/tenants/hermes_agent/api.js'),
  'utf8',
)
const styleSource = readFileSync(
  join(process.cwd(), 'src/features/tenants/hermes_agent/pages/ContentFactoryPage.css'),
  'utf8',
)

describe('content factory project creation', () => {
  it('creates projects only from a reviewed producer intent manifest', () => {
    expect(source).not.toContain('async function createProject(event)')
    expect(source).not.toContain('createContentFactoryProject')
    expect(apiSource).not.toContain('export async function createContentFactoryProject')
    expect(source).toContain('confirmContentFactoryProducerProject')
    expect(source).toContain('producerIntentSpec.intent_manifest')
  })

  it('starts new projects with the universal director and exposes both planning stages', () => {
    expect(source).toContain("'SERIES_DIRECTOR'")
    expect(source).toContain("'DIRECTOR'")
    expect(source).toContain("SERIES_DIRECTOR: '整批编导'")
    expect(source).toContain("DIRECTOR: '单条编导'")
    expect(source).toContain('videoSegmentLabel(videoModels, selected.config_json?.video_model)')
    expect(source).not.toContain('专业参数')
  })

  it('locks the immutable media contract after image or video production starts', () => {
    expect(source).toContain('function projectMediaContractLocked(project)')
    expect(source).toContain('disabled={mediaContractLocked}')
    expect(source).toContain('项目已进入媒体生产')
  })

  it('never defaults a restart to the removed legacy creative authoring stage', () => {
    expect(source).toContain("useState('DIRECTOR')")
    expect(source).toContain("? 'DIRECTOR' : selected.current_stage")
    expect(apiSource).toContain("stage: stage || 'DIRECTOR'")
    expect(apiSource).not.toContain("stage: stage || 'CREATIVE'")
  })

  it('lets the producer assistant own product binding and production parameters', () => {
    expect(source).toContain('关联商品（可选）')
    expect(source).toContain('执行参数摘要')
    expect(source).not.toContain('<span>是否绑定商品</span>')
  })

  it('does not require a live browser for API-first run or resume', () => {
    expect(source).toContain(
      "const canRun = selected && !busy && !['queued', 'running', 'generating_video', 'paused'].includes(selected.status)",
    )
    expect(source).not.toContain(
      'onClick={resumeProject} disabled={busy || !bridge.connected}',
    )
  })

  it('uses an AI producer as the default friendly project entry point', () => {
    expect(source).toContain('AI 制片助理')
    expect(source).toContain('说清目标，其余交给助理整理')
    expect(source).toContain("producerMessages.length ? '发送' : '开始沟通'")
    expect(source).toContain('PRODUCER_QUICK_STARTS')
    expect(source).toContain("label: '带货转化'")
    expect(source).toContain("label: '已有完整文案'")
    expect(source).toContain('await sendContentFactoryProducerTurn')
    expect(source).toContain('确认方案并创建 {producerProposal?.video_count || \'\'} 条视频任务')
    expect(source).toContain('await confirmContentFactoryProducerProject')
    expect(source).not.toContain('专业参数')
    expect(source).not.toContain('setProducerProductId((current) => current || String(productItems[0].id))')
    expect(source).toContain('商品选择已变化。请补充一句新要求')
  })

  it('keeps proposal and mutation as separate API operations', () => {
    expect(apiSource).toContain('/content-factory/producer/turn')
    expect(apiSource).toContain('/confirm`')
    expect(apiSource).toContain('proposal_sha256: proposalSha256')
    expect(apiSource).toContain('pending_decision_id: pendingDecisionId || null')
  })

  it('shows a professional effective-brief preview and supports exact natural-language confirmation', () => {
    expect(source).toContain('content-factory-brief')
    expect(source).toContain('producerIntentSpec.intent_manifest.requirements')
    expect(source).toContain('item.evidence_quote')
    expect(source).toContain('item.observable_checks')
    expect(source).toContain('producerIntentSpec.deliverables.map')
    expect(source).toContain('producerAuthoritativeScriptVersion')
    expect(source).toContain('isProducerConfirmationMessage(message)')
    expect(source).toContain('producerPendingDecisionId')
  })

  it('turns an explicit natural-language generation request into the reviewed project', () => {
    expect(source).toContain('function isProducerExecutionMessage(value)')
    expect(source).toContain('isProducerExecutionMessage(message)')
    expect(source).toContain("result.status === 'proposal_ready'")
    expect(source).toContain('result.proposal_sha256')
    expect(source).toContain('result.pending_decision_id')
    expect(source).toContain('已识别你的明确执行要求')
    expect(source).toContain('方案已整理完成，但项目创建暂时失败')
  })

  it('waits for long producer turns and recovers the committed reply without resubmitting', () => {
    expect(apiSource).toContain('{ timeout: 195_000 }')
    expect(source).toContain('PRODUCER_RECOVERY_DEADLINE_MS = 210_000')
    expect(source).toContain('async function recoverProducerTurn')
    expect(source).toContain("messages.length >= baselineMessageCount + 2 && lastMessage?.role === 'assistant'")
    expect(source).toContain('await recoverProducerTurn(wid, sessionKey, baselineMessageCount, startedAt)')
    expect(source).toContain('if (producerRequestMayStillBeRunning(err))')
    expect(source).toContain('error?.payload?.error?.code')
    expect(source).toContain("['ECONNABORTED', 'ERR_NETWORK', 'ETIMEDOUT']")
    expect(source).toContain("code === 'CONTENT_PRODUCER_RESPONSE_INVALID'")
    expect(source).toContain("recovered?.status === 'proposal_ready'")
    expect(source.match(/isProducerExecutionMessage\(message\)/g)?.length).toBeGreaterThanOrEqual(2)
    expect(source).toContain('内容模型供应商当前额度或线路不可用')
    expect(source).toContain('提示词被上游模型明确判定违反内容政策')
    expect(source).toContain('client_turn_id: clientTurnId')
    expect(source.match(/await sendContentFactoryProducerTurn\(/g)).toHaveLength(1)
  })

  it('lets the producer inspect scoped videos, images, and documents before confirmation', () => {
    expect(apiSource).toContain('uploadContentFactoryProducerAttachments')
    expect(apiSource).toContain('deleteContentFactoryProducerAttachment')
    expect(source).toContain('+ 上传对标视频')
    expect(source).toContain('+ 上传人物参考图')
    expect(source).toContain('+ 上传资料 / 图片')
    expect(source).toContain("uploadProducerAttachments(event, 'reference_video')")
    expect(source).toContain("uploadProducerAttachments(event, 'character_reference')")
    expect(source).toContain("uploadProducerAttachments(event, 'supporting_material')")
    expect(source).toContain("'.xlsx', '.xlsm', '.xltx', '.xltm'")
    expect(source).toContain("'.pptx', '.pptm', '.potx', '.potm', '.ppsx', '.ppsm'")
    expect(source).toContain("'.odt', '.ods', '.odp', '.pdf'")
    expect(source).toContain('Word、Excel、PowerPoint、PDF、OpenDocument')
    expect(source).toContain('请阅读并理解我刚上传的资料')
    expect(source).toContain('发送下一句话后，制片助理会结合附件一起理解')
    expect(source).toContain('producerAttachmentAnalysisPending')
    expect(source).toContain('window.setInterval(poll, 3_000)')
    expect(source).toContain('完整画面、口播、钩子、节奏、叙事和转化结构已拆解')
    expect(source).toContain("['queued', 'processing'].includes(item.analysis_status)")
    expect(source).toContain('producerAttachments.map')
  })

  it('accepts a public benchmark URL and waits for durable multimodal Producer continuation', () => {
    expect(apiSource).toContain('addContentFactoryProducerReferenceLink')
    expect(apiSource).toContain('/reference-links')
    expect(source).toContain('benchmarkVideoUrlFromText')
    expect(source).toContain('系统会在后台下载并做逐段多模态拆解')
    expect(source).toContain('contextMessage: message')
    expect(source).toContain('producer_turn_status')
    expect(source).toContain('完成后制片助理自动继续回复，不需要重复发送')
    expect(source).toContain('历史对标 · 本轮不使用')
  })

  it('isolates producer sessions by workspace and user and removes the old form draft', () => {
    expect(source).toContain('contentFactoryUserStorageKey')
    expect(source).toContain('`content-factory-${kind}:${wid}:user:${userId}`')
    expect(source).not.toContain("contentFactoryUserStorageKey('draft', wid, currentUserId)")
    expect(source).toContain("contentFactoryUserStorageKey('producer', wid, currentUserId)")
    expect(source).not.toContain('getItem(`content-factory-draft:${wid}`)')
    expect(source).not.toContain('getItem(`content-factory-producer:${wid}`)')
  })

  it('loads a scoped video-analysis handoff as a reviewable producer draft', () => {
    expect(source).toContain("searchParams.get('producer_session')")
    expect(source).toContain('savedKey = handoffSessionKey')
    expect(source).toContain('session?.draft_message')
    expect(source).toContain("session?.source_context?.type === 'tiktok_shop_video_analysis'")
    expect(source).toContain('视频分析报告和本地参考视频已导入')
    expect(source).toContain("nextParams.delete('producer_session')")
  })

  it('keeps completed project conversations open for additions and revisions', () => {
    expect(source).toContain('已创建 · 可继续沟通')
    expect(source).toContain("producerStatus === 'created' ? '发送新要求'")
    expect(source).toContain('继续沟通')
    expect(source).toContain('continueProducerConversationForProject(selected)')
    expect(source).toContain('原项目成果不会被覆盖')
    expect(source).not.toContain("producerStatus !== 'created' ? (")
    expect(source).not.toContain("disabled={producerBusy || producerStatus === 'created'}")
  })

  it('keeps the producer composer visible and restores focus from a project', () => {
    expect(source).toContain("import './ContentFactoryPage.css'")
    expect(source).toContain('content-factory-project-rail')
    expect(source).toContain('content-factory-producer-panel')
    expect(source).toContain('content-factory-production-panel')
    expect(source).toContain('ref={producerComposerRef}')
    expect(source).toContain('focusProducerComposer()')
    expect(source).toContain("producerPanelRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })")
    expect(source).toContain("producerComposerRef.current?.focus?.({ preventScroll: true })")
    expect(source).toContain('onKeyDown={handleProducerComposerKeyDown}')
    expect(source).toContain('Enter 发送 · Shift+Enter 换行')
  })

  it('uses a conversation-first workspace instead of squeezing chat beside project details', () => {
    expect(source).toContain("useState('conversation')")
    expect(source).toContain('需求沟通工作台')
    expect(source).toContain('与制片助理沟通')
    expect(source).toContain('查看项目执行')
    expect(source).toContain('is-${workspaceMode}-mode')
    expect(styleSource).toContain('.content-factory-workspace-layout.is-conversation-mode .content-factory-production-panel')
    expect(styleSource).toContain('.content-factory-workspace-layout.is-production-mode .content-factory-producer-panel')
    expect(styleSource).toContain('min-height: min(52dvh, 560px)')
    expect(styleSource).not.toContain('grid-template-columns: minmax(210px, 260px) minmax(390px, 470px)')
  })

  it('does not expose retired creative-stage or single-duration compatibility paths', () => {
    expect(source).not.toContain("CREATIVE: '旧版创意")
    expect(source).not.toContain('config.video_duration_seconds')
    expect(source).not.toContain('selected.config_json?.video_duration_seconds')
    expect(source).not.toContain('remove legacy unscoped session')
  })

  it('keeps browser slots and technical details out of the primary workflow', () => {
    expect(source).toContain('content-factory-system-drawer')
    expect(source).toContain('API 优先；只有需要浏览器兜底或维护登录时才使用 Slot')
    expect(source).toContain('content-factory-project-specs')
    expect(source).toContain('查看制作规格')
  })

  it('shows automatic Bridge updates with a personalized manual installer fallback', () => {
    expect(source).toContain('device.agent_update_required')
    expect(source).toContain("device.agent_update_state === 'failed'")
    expect(source).toContain('版本不一致，客户端正在自动更新。')
    expect(source).toContain('版本不一致且客户端离线，请手动更新。')
    expect(source).toContain('下载最新版 EXE')
    expect(source).toContain('requestedDevice?.device_id || getBridgeDeviceId(wid)')
  })
})
