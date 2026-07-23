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

describe('content factory project creation', () => {
  it('submits projects to the durable backend queue even when every browser slot is occupied', () => {
    const createStart = source.indexOf('async function createProject(event)')
    const createEnd = source.indexOf('async function createProduct(event)', createStart)
    const createHandler = source.slice(createStart, createEnd)

    expect(createStart).toBeGreaterThan(-1)
    expect(createEnd).toBeGreaterThan(createStart)
    expect(createHandler).toContain('await createContentFactoryProject(wid, payload)')
    expect(createHandler).not.toContain('hasFreeLoggedInSlot')
    expect(createHandler).not.toContain('usage_status')
  })

  it('starts new projects with the universal director and exposes both planning stages', () => {
    expect(source).toContain("content_director_mode: 'enforce'")
    expect(source).toContain("'SERIES_DIRECTOR'")
    expect(source).toContain("'DIRECTOR'")
    expect(source).toContain("SERIES_DIRECTOR: '整批编导'")
    expect(source).toContain("DIRECTOR: '单条编导'")
    expect(source).toContain('max_api_video_variants_in_flight: 2')
    expect(source).toContain('<span>同时生成视频数</span>')
    expect(source).toContain("video_aspect_ratio: '9:16'")
    expect(source).toContain('<option value="16:9">16:9 横屏</option>')
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

  it('treats product binding as a constraint instead of a content template', () => {
    expect(source).toContain('<span>是否绑定商品</span>')
    expect(source).toContain('<option value="general">不绑定商品</option>')
    expect(source).toContain('可生成故事、科普、教程、演示、对比、访谈、动画等')
    expect(source).not.toContain('<span>内容类型</span>')
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
    expect(source).toContain("useState('assistant')")
    expect(source).toContain('AI制片助理')
    expect(source).toContain('像聊天一样描述目标即可')
    expect(source).toContain('开始和制片助理沟通')
    expect(source).toContain('PRODUCER_QUICK_STARTS')
    expect(source).toContain("label: '带货转化'")
    expect(source).toContain("label: '已有完整文案'")
    expect(source).toContain('await sendContentFactoryProducerTurn')
    expect(source).toContain('确认并创建 {producerProposal?.video_count || \'\'} 条视频任务')
    expect(source).toContain('await confirmContentFactoryProducerProject')
    expect(source).toContain("creationMode === 'advanced' ? 'grid' : 'none'")
    expect(source).not.toContain('setProducerProductId((current) => current || String(productItems[0].id))')
    expect(source).toContain('商品选择已变化。请补充一句新要求')
  })

  it('keeps proposal and mutation as separate API operations', () => {
    expect(apiSource).toContain('/content-factory/producer/turn')
    expect(apiSource).toContain('/confirm`')
    expect(apiSource).toContain('{ proposal_sha256: proposalSha256 }')
  })

  it('waits for long producer turns and recovers the committed reply without resubmitting', () => {
    expect(apiSource).toContain('{ timeout: 195_000 }')
    expect(source).toContain('PRODUCER_RECOVERY_DEADLINE_MS = 210_000')
    expect(source).toContain('async function recoverProducerTurn')
    expect(source).toContain("messages.length >= baselineMessageCount + 2 && lastMessage?.role === 'assistant'")
    expect(source).toContain('await recoverProducerTurn(wid, sessionKey, baselineMessageCount, startedAt)')
    expect(source).toContain('if (producerRequestMayStillBeRunning(err))')
    expect(source).toContain('内容模型供应商当前额度或线路不可用')
    expect(source).toContain('提示词被上游模型明确判定违反内容政策')
    expect(source).toContain('client_turn_id: clientTurnId')
    expect(source.match(/await sendContentFactoryProducerTurn\(/g)).toHaveLength(1)
  })

  it('lets the producer inspect scoped benchmark and character attachments before confirmation', () => {
    expect(apiSource).toContain('uploadContentFactoryProducerAttachments')
    expect(apiSource).toContain('deleteContentFactoryProducerAttachment')
    expect(source).toContain('+ 上传对标视频')
    expect(source).toContain('+ 上传人物参考图')
    expect(source).toContain("uploadProducerAttachments(event, 'reference_video')")
    expect(source).toContain("uploadProducerAttachments(event, 'character_reference')")
    expect(source).toContain('发送下一句话后，制片助理会结合附件一起理解')
    expect(source).toContain('producerAttachmentAnalysisPending')
    expect(source).toContain('window.setInterval(poll, 3_000)')
    expect(source).toContain('关键画面和口播文案已分析完成')
    expect(source).toContain("['queued', 'processing'].includes(item.analysis_status)")
    expect(source).toContain('producerAttachments.map')
  })

  it('isolates browser drafts and producer sessions by workspace and user', () => {
    expect(source).toContain('contentFactoryUserStorageKey')
    expect(source).toContain('`content-factory-${kind}:${wid}:user:${userId}`')
    expect(source).toContain("contentFactoryUserStorageKey('draft', wid, currentUserId)")
    expect(source).toContain("contentFactoryUserStorageKey('producer', wid, currentUserId)")
    expect(source).not.toContain('getItem(`content-factory-draft:${wid}`)')
    expect(source).not.toContain('getItem(`content-factory-producer:${wid}`)')
  })
})
