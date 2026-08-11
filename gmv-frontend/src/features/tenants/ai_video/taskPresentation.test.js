import { describe, expect, it } from 'vitest'
import {
  aiVideoTaskProgressDetail,
  aiVideoTaskStatus,
  isAiVideoTaskFailure,
} from './taskPresentation.js'

describe('AI video task presentation', () => {
  it('collapses provider states into four concise business statuses', () => {
    expect(aiVideoTaskStatus('queued_local').label).toBe('排队中')
    expect(aiVideoTaskStatus('waiting_dependency').label).toBe('排队中')
    expect(aiVideoTaskStatus('submitting').label).toBe('正在生成')
    expect(aiVideoTaskStatus('downloading').label).toBe('正在生成')
    expect(aiVideoTaskStatus('success').label).toBe('成功')
    expect(aiVideoTaskStatus('timeout').label).toBe('失败')
  })

  it('keeps technical progress out of the status badge', () => {
    const task = {
      state: 'queued_local',
      status_message: '正在快速切换可用账号',
    }
    expect(aiVideoTaskStatus(task).label).toBe('排队中')
    expect(aiVideoTaskProgressDetail(task)).toBe('正在快速切换可用账号')
  })

  it('recognizes every terminal failure presentation', () => {
    for (const state of ['failed', 'error', 'timeout', 'cancelled']) {
      expect(isAiVideoTaskFailure(state)).toBe(true)
    }
    expect(isAiVideoTaskFailure('submitting')).toBe(false)
  })
})
