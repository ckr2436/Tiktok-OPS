import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { shouldPollByState, taskStatusLabel } from './GenerateVideoPage.jsx'

const source = readFileSync(
  join(process.cwd(), 'src/features/tenants/ai_video/pages/GenerateVideoPage.jsx'),
  'utf8',
)

describe('AI video local draft isolation', () => {
  it('keeps polling every non-terminal task state including submitting', () => {
    for (const state of [
      undefined,
      'queued_local',
      'submitting',
      'submitted',
      'waiting_dependency',
      'queued',
      'in_progress',
      'generating',
      'downloading',
      'future_provider_state',
    ]) {
      expect(shouldPollByState(state)).toBe(true)
    }

    for (const state of [
      'success',
      'succeeded',
      'complete',
      'completed',
      'ok',
      'failed',
      'fail',
      'error',
      'timeout',
      'cancelled',
      'canceled',
    ]) {
      expect(shouldPollByState(state)).toBe(false)
    }
  })

  it('scopes task and file draft keys by workspace and authenticated user', () => {
    expect(source).toContain('const currentUserId = sessionQuery.data?.id')
    expect(source).toContain('`${LAST_TASK_KEY_PREFIX}${wid}_u${userId}_${modelId}`')
    expect(source).toContain('`${FORM_STATE_KEY_PREFIX}${wid}_u${userId}`')
    expect(source).toContain('getLastTaskKey(wid, currentUserId, modelId)')
    expect(source).toContain('getFormStateKey(wid, currentUserId)')
    expect(source).not.toContain('`${FORM_STATE_KEY_PREFIX}${wid ?? \'\'}`')
  })

  it('derives reference and duration controls from the enabled provider routes', () => {
    expect(source).toContain('routingReferenceImageLimit(')
    expect(source).toContain('routingDurationOptions(')
    expect(source).toContain('referenceImageLimit: 10')
    expect(source).toContain('Array.from({ length: 12 }, (_, index) => index + 4)')
    expect(source).not.toContain('referenceImageLimit: 9')
  })

  it('shows provider-aware progress instead of the raw queued_local state', () => {
    expect(source).toContain('export function taskStatusLabel(task)')
    expect(source).toContain('{taskStatusLabel(task)}')
    expect(source).toContain('{taskStatusLabel(t)}')
    expect(taskStatusLabel({ state: 'queued_local', status_message: '很长的供应商调度说明' })).toBe('排队中')
    expect(taskStatusLabel({ state: 'submitting' })).toBe('正在生成')
    expect(taskStatusLabel({ state: 'downloading' })).toBe('正在生成')
    expect(taskStatusLabel({ state: 'success' })).toBe('成功')
    expect(taskStatusLabel({ state: 'failed', status_message: '很长的错误原因' })).toBe('失败')
  })

  it('shows and enforces the verified Seedance prompt budget without truncation', () => {
    expect(source).toContain('const SEEDANCE_PROMPT_MAX_LEN = 495')
    expect(source).toContain('maxLength={promptMaxLength}')
    expect(source).toContain('系统不会静默截断')
    expect(source).not.toContain(".trim().slice(0, MAX_PROMPT_LEN)")
  })
})
