// src/features/platform/admin/pages/BcAdsPlanConfig.jsx
import { useEffect, useMemo, useState } from 'react'
import {
  DEFAULT_PLAN_CONFIG,
  toEditableConfig,
  toApiPayload,
} from '../../../bc_ads_shop_product/planDefaults.js'
import {
  fetchPlanConfig,
  savePlanConfig,
  publishPlanSnapshot,
} from '../bc_ads_shop_product/service.js'

const EMPTY_PLAN_TEMPLATE = {
  id: '',
  title: '',
  objective: '',
  audience: '',
  focus: '',
  cadence: '',
  keyActions: '',
  deliverables: '',
  metrics: '',
  notes: '',
}

function formatDateTime(value) {
  if (!value) return ''
  try {
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return ''
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  } catch {
    return ''
  }
}

function PlanEditorCard({ index, plan, onChange, onRemove, disableRemove }) {
  const updateField = (field) => (event) => {
    const value = event?.target?.value ?? ''
    onChange({ ...plan, [field]: value })
  }

  const planId = plan.id || `plan-${index + 1}`

  return (
    <section className="plan-card" aria-labelledby={`${planId}-title`}>
      <header className="plan-card__head">
        <div className="plan-card__meta">
          <span className="plan-card__badge">阶段 {index + 1}</span>
          <input
            id={`${planId}-title`}
            className="input plan-card__title"
            placeholder="请输入任务模块名称"
            value={plan.title}
            onChange={updateField('title')}
          />
        </div>
        <div className="plan-card__actions">
          <button
            type="button"
            className="btn ghost sm"
            onClick={() => onRemove(index)}
            disabled={disableRemove}
          >
            移除
          </button>
        </div>
      </header>

      <div className="plan-card__grid">
        <label className="form-field">
          <span className="form-field__label">核心目标</span>
          <textarea
            className="input"
            rows={3}
            placeholder="例如：建立首月 GMV 拆解与关键指标体系"
            value={plan.objective}
            onChange={updateField('objective')}
          />
        </label>

        <label className="form-field">
          <span className="form-field__label">适用角色 / 团队</span>
          <input
            className="input"
            placeholder="运营负责人 / 广告投放 / 客服等"
            value={plan.audience}
            onChange={updateField('audience')}
          />
        </label>

        <label className="form-field">
          <span className="form-field__label">阶段重点</span>
          <input
            className="input"
            placeholder="如：诊断、投放排期、复购运营"
            value={plan.focus}
            onChange={updateField('focus')}
          />
        </label>

        <label className="form-field">
          <span className="form-field__label">建议频次 / 周期</span>
          <input
            className="input"
            placeholder="例如：执行期 2 周 · 每 3 天复盘"
            value={plan.cadence}
            onChange={updateField('cadence')}
          />
        </label>

        <label className="form-field plan-card__field--full">
          <span className="form-field__label">执行要点（每行一条）</span>
          <textarea
            className="input"
            rows={4}
            placeholder="配置广告计划\n同步直播节奏\n上线重定向方案"
            value={plan.keyActions}
            onChange={updateField('keyActions')}
          />
        </label>

        <label className="form-field">
          <span className="form-field__label">产出物 / 交付件</span>
          <textarea
            className="input"
            rows={3}
            placeholder="广告排期表\n素材包\n诊断报告"
            value={plan.deliverables}
            onChange={updateField('deliverables')}
          />
        </label>

        <label className="form-field">
          <span className="form-field__label">衡量指标</span>
          <textarea
            className="input"
            rows={3}
            placeholder="GMV\nCTR\n复购率"
            value={plan.metrics}
            onChange={updateField('metrics')}
          />
        </label>

        <label className="form-field plan-card__field--full">
          <span className="form-field__label">备注 / 联动提醒</span>
          <textarea
            className="input"
            rows={2}
            placeholder="如需平台顾问介入、跨部门协同等说明"
            value={plan.notes}
            onChange={updateField('notes')}
          />
        </label>
      </div>
    </section>
  )
}

export default function BcAdsPlanConfig() {
  const [form, setForm] = useState(() => toEditableConfig(DEFAULT_PLAN_CONFIG))
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [lastSavedAt, setLastSavedAt] = useState('')

  useEffect(() => {
    let mounted = true
    async function load() {
      setLoading(true)
      setError('')
      try {
        const data = await fetchPlanConfig()
        if (!mounted) return
        setForm(toEditableConfig(data || {}))
        setLastSavedAt(data?.updated_at ?? data?.updatedAt ?? '')
      } catch (err) {
        console.error('Failed to load bc-ads plan config', err)
        if (!mounted) return
        setForm(toEditableConfig(DEFAULT_PLAN_CONFIG))
        setError('加载配置失败，已载入默认模板，请保存后重新发布。')
      } finally {
        if (mounted) {
          setLoading(false)
        }
      }
    }
    load()
    return () => {
      mounted = false
    }
  }, [])

  const planCount = form.plans?.length ?? 0
  const invalidPlans = useMemo(() => {
    if (!Array.isArray(form.plans)) return true
    return form.plans.some((plan) => !plan.title?.trim() || !plan.objective?.trim())
  }, [form.plans])

  const disableSave = saving || invalidPlans

  function updatePlan(index, nextPlan) {
    setForm((prev) => {
      const plans = Array.isArray(prev.plans) ? [...prev.plans] : []
      plans[index] = { ...plans[index], ...nextPlan }
      return { ...prev, plans }
    })
  }

  function addPlan() {
    setForm((prev) => {
      const next = Array.isArray(prev.plans) ? [...prev.plans] : []
      const template = { ...EMPTY_PLAN_TEMPLATE }
      template.id = `plan-${Date.now()}`
      next.push(template)
      return { ...prev, plans: next }
    })
  }

  function removePlan(index) {
    setForm((prev) => {
      const plans = Array.isArray(prev.plans) ? [...prev.plans] : []
      plans.splice(index, 1)
      return { ...prev, plans }
    })
  }

  function resetToDefault() {
    if (!confirm('确定恢复到平台默认模板吗？未保存的更改将丢失。')) return
    setForm(toEditableConfig(DEFAULT_PLAN_CONFIG))
    setError('')
    setNotice('已恢复默认模板，请记得保存。')
  }

  function handleCooldownChange(event) {
    const value = Number(event?.target?.value ?? 0)
    if (!Number.isFinite(value)) return
    const clamped = Math.min(Math.max(value, 5), 1440)
    setForm((prev) => ({
      ...prev,
      syncCooldownMinutes: clamped,
    }))
  }

  async function handleSave() {
    setSaving(true)
    setNotice('')
    try {
      const payload = toApiPayload(form)
      await savePlanConfig(payload)
      setNotice('配置已保存，租户手动同步后即可收到最新模板。')
      setError('')
      setLastSavedAt(new Date().toISOString())
    } catch (err) {
      console.error('Failed to save bc-ads plan config', err)
      setError('保存失败，请稍后重试。')
    } finally {
      setSaving(false)
    }
  }

  async function handlePublish() {
    setPublishing(true)
    setNotice('')
    try {
      const payload = toApiPayload(form)
      await publishPlanSnapshot(payload)
      setNotice('已向全部租户下发最新计划任务模板。')
      setError('')
      setLastSavedAt(new Date().toISOString())
    } catch (err) {
      console.error('Failed to publish bc-ads plan snapshot', err)
      setError('下发失败，请确认网络或稍后再试。')
    } finally {
      setPublishing(false)
    }
  }

  return (
    <div className="page-with-gap">
      <div className="card">
        <h2 style={{ marginTop: 0 }}>BC Ads · 运营计划任务配置</h2>
        <p>
          该配置用于生成「bc-ads-shop-product」专业版运营计划模板，租户可在工作台手动同步。
          建议在每次版本调整后保存并下发，保障各租户执行口径一致。
        </p>
        <ul className="plan-intro-list">
          <li>支持自定义阶段任务、执行要点与衡量指标。</li>
          <li>可限制租户手动同步频率（单位：分钟），避免高频刷新导致负载。</li>
          <li>保存仅更新平台侧草稿，下发将推送至全部租户。</li>
        </ul>
        {lastSavedAt && (
          <p className="small-muted">最近保存：{formatDateTime(lastSavedAt)}</p>
        )}
      </div>

      <div className="card plan-card-wrapper">
        <div className="form-field">
          <span className="form-field__label">手动同步冷却时间（分钟）</span>
          <input
            type="number"
            min={5}
            max={1440}
            className="input"
            value={form.syncCooldownMinutes}
            onChange={handleCooldownChange}
          />
          <span className="small-muted">
            限制租户触发手动同步的频率，建议在 30-120 分钟范围内配置。
          </span>
        </div>

        {error && <div className="alert alert--error">{error}</div>}
        {notice && !error && <div className="alert">{notice}</div>}

        {loading ? (
          <div className="plan-loading">配置加载中…</div>
        ) : (
          <div className="plan-card-list">
            {Array.isArray(form.plans) && form.plans.length > 0 ? (
              form.plans.map((plan, idx) => (
                <PlanEditorCard
                  key={plan.id || idx}
                  index={idx}
                  plan={plan}
                  onChange={(next) => updatePlan(idx, next)}
                  onRemove={removePlan}
                  disableRemove={planCount <= 1}
                />
              ))
            ) : (
              <div className="plan-empty">暂无任务模块，请添加新的计划阶段。</div>
            )}
          </div>
        )}

        <div className="plan-toolbar">
          <button type="button" className="btn ghost" onClick={addPlan}>
            新增计划阶段
          </button>
          <button type="button" className="btn ghost" onClick={resetToDefault}>
            恢复默认模板
          </button>
        </div>

        <div className="actions plan-actions">
          <button
            type="button"
            className="btn ghost"
            onClick={handlePublish}
            disabled={publishing || invalidPlans}
          >
            {publishing ? '下发中…' : '保存并下发'}
          </button>
          <button
            type="button"
            className="btn"
            onClick={handleSave}
            disabled={disableSave}
          >
            {saving ? '保存中…' : '仅保存草稿'}
          </button>
        </div>
      </div>
    </div>
  )
}
