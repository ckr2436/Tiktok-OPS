import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import Loading from '../../../../components/ui/Loading.jsx'
import { getAutomationControlCenter } from '../service.js'

const HEALTH_LABELS = {
  healthy: '正常',
  delayed: '延迟',
  error: '错误',
  waiting: '待运行',
  disabled: '已停用',
  legacy: '历史任务',
}

const LEVEL_LABELS = {
  OVERVIEW_HOURLY: '账户总览',
  OVERVIEW_DAILY: '总览日终对账',
  CAMPAIGN_HOURLY: '计划指标',
  CAMPAIGN_DAILY: '计划日终对账',
  PRODUCT_HOURLY: '商品指标',
  PRODUCT_DAILY: '商品日终对账',
  CREATIVE_10MIN: '创意素材指标',
  LIVESTREAM_HOURLY: '直播指标',
  LIVESTREAM_DAILY: '直播日终对账',
  DURATION_HOURLY: '时长指标',
  DURATION_DAILY: '时长日终对账',
}

function formatDate(value) {
  if (!value) return '尚无记录'
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(new Date(value))
  } catch (_err) {
    return String(value)
  }
}

function formatCadence(seconds) {
  const value = Number(seconds || 0)
  if (!value) return '按需'
  if (value < 60) return `${value} 秒`
  if (value % 3600 === 0) return `${value / 3600} 小时`
  return `${Math.round(value / 60)} 分钟`
}

function formatLag(seconds) {
  if (seconds === null || seconds === undefined) return '尚无数据'
  const value = Number(seconds)
  if (value < 60) return `${value} 秒`
  if (value < 3600) return `${Math.floor(value / 60)} 分钟`
  return `${Math.floor(value / 3600)} 小时 ${Math.floor((value % 3600) / 60)} 分钟`
}

function StatusBadge({ status }) {
  const normalized = HEALTH_LABELS[status] ? status : 'waiting'
  return (
    <span className={`platform-ops__status is-${normalized}`}>
      <span aria-hidden="true" />
      {HEALTH_LABELS[normalized]}
    </span>
  )
}

function Metric({ label, value, note }) {
  return (
    <div className="platform-ops__metric">
      <span>{label}</span>
      <strong>{value}</strong>
      {note ? <small>{note}</small> : null}
    </div>
  )
}

function AutomationCard({ item }) {
  return (
    <article className="platform-ops__automation-card">
      <header>
        <h3>{item.name}</h3>
        <StatusBadge status={item.status} />
      </header>
      <p>{item.detail}</p>
      <dl>
        <div>
          <dt>检查周期</dt>
          <dd>{formatCadence(item.cadence_seconds)}</dd>
        </div>
        <div>
          <dt>最近活动</dt>
          <dd>{formatDate(item.last_activity_at)}</dd>
        </div>
      </dl>
    </article>
  )
}

export default function AutomationControlCenterPage() {
  const [showAdvanced, setShowAdvanced] = useState(false)
  const controlQuery = useQuery({
    queryKey: ['platform', 'gmvmax', 'automation-control-center'],
    queryFn: getAutomationControlCenter,
    refetchInterval: 30_000,
  })

  const data = controlQuery.data || {}
  const schedules = Array.isArray(data.schedules) ? data.schedules : []
  const currentSchedules = useMemo(
    () => schedules.filter((item) => !item.workspace_deleted),
    [schedules],
  )
  const legacySchedules = useMemo(
    () => schedules.filter((item) => item.workspace_deleted),
    [schedules],
  )
  const workspaceGroups = useMemo(() => {
    const groups = new Map()
    currentSchedules.forEach((item) => {
      const key = String(item.workspace_id)
      if (!groups.has(key)) {
        groups.set(key, {
          id: item.workspace_id,
          name: item.workspace_name,
          items: [],
        })
      }
      groups.get(key).items.push(item)
    })
    return Array.from(groups.values())
  }, [currentSchedules])

  if (controlQuery.isLoading) {
    return <Loading text="正在读取自动化运行状态" />
  }

  if (controlQuery.error) {
    return (
      <div className="alert alert--error">
        自动化运行状态加载失败。
        <button className="btn ghost" onClick={() => controlQuery.refetch()}>重试</button>
      </div>
    )
  }

  const profile = data.profile || {}
  const summary = data.summary || {}
  const guardrail = data.api_guardrail || {}
  const health = summary.schedule_health || {}

  return (
    <main className="platform-ops">
      <header className="platform-ops__page-header">
        <div>
          <div className="platform-ops__title-row">
            <h1>自动化运行中心</h1>
            <span className="platform-ops__managed">平台统一管理</span>
          </div>
          <p>生产默认配置</p>
        </div>
        <div className="platform-ops__actions">
          <Link className="btn ghost" to="/platform/policies">API 护栏</Link>
          <button className="btn" onClick={() => controlQuery.refetch()} disabled={controlQuery.isFetching}>
            {controlQuery.isFetching ? '刷新中' : '刷新状态'}
          </button>
        </div>
      </header>

      {(data.warnings || []).length ? (
        <section className="platform-ops__warnings" aria-label="运行提醒">
          {(data.warnings || []).map((warning) => (
            <div key={warning.code} className={`platform-ops__warning is-${warning.severity || 'info'}`}>
              <strong>{warning.severity === 'warning' ? '需要处理' : '配置提示'}</strong>
              <span>{warning.message}</span>
            </div>
          ))}
        </section>
      ) : null}

      <section className="platform-ops__band">
        <header className="platform-ops__section-header">
          <div>
            <h2>全局生产配置</h2>
            <span>租户不可自定义</span>
          </div>
          <StatusBadge status={(health.error || health.delayed) ? 'delayed' : 'healthy'} />
        </header>
        <div className="platform-ops__metrics">
          <Metric
            label="TikTok API"
            value={`${profile.api_runtime_qps ?? '-'} / ${profile.api_approved_qps ?? '-'} QPS`}
            note="当前稳定速率 / 获批上限"
          />
          <Metric label="实时止损" value={formatCadence(profile.smart_guard_seconds)} note="Smart Guard" />
          <Metric label="素材守护" value={formatCadence(profile.creative_guard_seconds)} note="Creative Guard" />
          <Metric label="Hermes 复核" value={formatCadence(profile.hermes_advisor_seconds)} note="动作级审批" />
          <Metric label="托管任务" value={profile.active_schedule_count ?? 0} note="当前有效实例" />
        </div>
      </section>

      <section className="platform-ops__band">
        <header className="platform-ops__section-header">
          <div>
            <h2>自动化引擎</h2>
            <span>最近更新 {formatDate(data.generated_at)}</span>
          </div>
        </header>
        <div className="platform-ops__automation-grid">
          {(data.automation || []).map((item) => <AutomationCard key={item.key} item={item} />)}
        </div>
      </section>

      <section className="platform-ops__band">
        <header className="platform-ops__section-header">
          <div>
            <h2>数据同步健康</h2>
            <span>{summary.workspaces ?? 0} 个有效公司</span>
          </div>
          <div className="platform-ops__health-summary">
            <span>正常 {health.healthy || 0}</span>
            <span>延迟 {health.delayed || 0}</span>
            <span>错误 {health.error || 0}</span>
          </div>
        </header>

        <div className="platform-ops__workspace-list">
          {workspaceGroups.map((group) => (
            <section key={group.id} className="platform-ops__workspace">
              <header>
                <div>
                  <h3>{group.name}</h3>
                  <span>平台托管实例 {group.items.length}</span>
                </div>
              </header>
              <div className="platform-ops__schedule-table-wrap">
                <table className="platform-ops__schedule-table">
                  <thead>
                    <tr>
                      <th>数据任务</th>
                      <th>状态</th>
                      <th>同步周期</th>
                      <th>数据延迟</th>
                      <th>最近成功</th>
                      <th>下次调度</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.items.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <strong>{LEVEL_LABELS[item.level] || item.task_name || item.category}</strong>
                          <small>{item.level || item.task_name}</small>
                        </td>
                        <td><StatusBadge status={item.health} /></td>
                        <td>{formatCadence(Number(item.interval_minutes) * 60)}</td>
                        <td>{formatLag(item.lag_seconds)}</td>
                        <td>{formatDate(item.last_success_at)}</td>
                        <td>{formatDate(item.next_run_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ))}
        </div>
      </section>

      <section className="platform-ops__band platform-ops__api-summary">
        <header className="platform-ops__section-header">
          <div>
            <h2>API 护栏摘要</h2>
            <span>全平台生效</span>
          </div>
          <Link className="btn ghost" to="/platform/policies">高级配置</Link>
        </header>
        <div className="platform-ops__metrics">
          <Metric label="运行速率" value={`${guardrail.runtime_default_qps ?? '-'} QPS`} />
          <Metric label="获批上限" value={`${guardrail.approved_qps ?? '-'} QPS`} />
          <Metric label="启用策略" value={guardrail.active_policy_count ?? 0} />
          <Metric label="有效限流" value={guardrail.effective_qps ? `${guardrail.effective_qps} QPS` : '未配置'} />
        </div>
      </section>

      <section className="platform-ops__advanced">
        <button
          type="button"
          className="platform-ops__advanced-toggle"
          aria-expanded={showAdvanced}
          onClick={() => setShowAdvanced((value) => !value)}
        >
          <span>高级诊断</span>
          <span>{showAdvanced ? '收起' : '展开'}</span>
        </button>
        {showAdvanced ? (
          <div className="platform-ops__advanced-body">
            <p>内部标识仅用于排障，任务参数由平台生产配置统一维护。</p>
            <div className="platform-ops__raw-grid">
              {schedules.map((item) => (
                <article key={item.id} className="platform-ops__raw-item">
                  <header>
                    <strong>#{item.id} {item.level || item.task_name}</strong>
                    <StatusBadge status={item.health} />
                  </header>
                  <span>{item.workspace_name} · workspace {item.workspace_id} · auth {item.auth_id || '-'}</span>
                  <span>{item.last_error || '无当前错误'}</span>
                </article>
              ))}
            </div>
            {legacySchedules.length ? <p>历史公司任务：{legacySchedules.length} 条，建议归档。</p> : null}
          </div>
        ) : null}
      </section>
    </main>
  )
}
