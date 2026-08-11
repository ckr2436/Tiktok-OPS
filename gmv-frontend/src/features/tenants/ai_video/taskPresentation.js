const SUCCESS_STATES = new Set([
  'success',
  'succeeded',
  'complete',
  'completed',
  'ok',
])

const FAILURE_STATES = new Set([
  'failed',
  'fail',
  'error',
  'timeout',
  'cancelled',
  'canceled',
])

const QUEUED_STATES = new Set([
  '',
  'queued_local',
  'queued',
  'queuing',
  'pending',
  'waiting',
  'waiting_dependency',
])

export function aiVideoTaskStatus(taskOrState) {
  const state = String(
    typeof taskOrState === 'object' ? taskOrState?.state : taskOrState,
  ).trim().toLowerCase()

  if (SUCCESS_STATES.has(state)) {
    return { key: 'success', label: '成功', tone: 'success', terminal: true }
  }
  if (FAILURE_STATES.has(state)) {
    return { key: 'failed', label: '失败', tone: 'fail', terminal: true }
  }
  if (QUEUED_STATES.has(state)) {
    return { key: 'queued', label: '排队中', tone: 'waiting', terminal: false }
  }
  return { key: 'running', label: '正在生成', tone: 'running', terminal: false }
}

export function aiVideoTaskProgressDetail(task) {
  const status = aiVideoTaskStatus(task)
  if (status.terminal) return ''

  const message = String(task?.status_message || '').trim()
  if (message && message !== status.label) return message

  const state = String(task?.state || '').trim().toLowerCase()
  if (state === 'queued_local') return '正在选择可用供应商'
  if (state === 'waiting_dependency') return '正在等待前置任务'
  if (state === 'submitting') return '正在提交给视频模型'
  if (state === 'downloading') return '视频已生成，正在保存到本地'
  return ''
}

export function isAiVideoTaskFailure(taskOrState) {
  return aiVideoTaskStatus(taskOrState).key === 'failed'
}
