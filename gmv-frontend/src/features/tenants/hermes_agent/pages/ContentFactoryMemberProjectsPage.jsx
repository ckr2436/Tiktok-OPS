import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { useAppSelector } from '../../../../app/hooks.js'
import Loading from '../../../../components/ui/Loading.jsx'
import Modal from '../../../../components/ui/Modal.jsx'
import { listTenantUsers } from '../../users/service.js'
import {
  adminContentFactoryAssetUrl,
  adminContentFactoryDeliverablesZipUrl,
  fetchAdminContentFactoryProject,
  fetchAdminContentFactoryProjects,
} from '../api.js'

function creatorLabel(project) {
  const label = project?.created_by_label || `用户 #${project?.user_id || '-'}`
  if (project?.created_by_usercode && label !== project.created_by_usercode) {
    return `${label}（${project.created_by_usercode}）`
  }
  return label
}

function statusColor(status) {
  const value = String(status || '').toLowerCase()
  if (value === 'complete') return '#16803c'
  if (value.includes('fail')) return '#c62828'
  if (value.includes('run') || value.includes('queue') || value.includes('generat')) return '#1d63d8'
  return '#667085'
}

export default function ContentFactoryMemberProjectsPage() {
  const { wid } = useParams()
  const session = useAppSelector((state) => state.session?.data || {})
  const role = String(session?.role || '').toLowerCase()
  const isTenantAdmin = role === 'owner' || role === 'admin'
  const [creatorUserId, setCreatorUserId] = useState('')
  const [selectedKey, setSelectedKey] = useState('')
  const [preview, setPreview] = useState(null)

  const usersQuery = useQuery({
    queryKey: ['tenant-users-for-content-factory-admin', wid],
    queryFn: () => listTenantUsers({ wid, page: 1, size: 200 }),
    enabled: !!wid && isTenantAdmin,
    staleTime: 60_000,
  })

  const projectsQuery = useQuery({
    queryKey: ['admin-content-factory-projects', wid, creatorUserId],
    queryFn: () => fetchAdminContentFactoryProjects(wid, {
      creator_user_id: creatorUserId || undefined,
    }),
    enabled: !!wid && isTenantAdmin,
    refetchInterval: 15_000,
  })

  const detailQuery = useQuery({
    queryKey: ['admin-content-factory-project', wid, selectedKey],
    queryFn: () => fetchAdminContentFactoryProject(wid, selectedKey),
    enabled: !!wid && !!selectedKey && isTenantAdmin,
    refetchInterval: 10_000,
  })

  const projects = projectsQuery.data || []
  const users = usersQuery.data?.items || []
  const selectedSummary = useMemo(
    () => projects.find((project) => project.project_key === selectedKey),
    [projects, selectedKey],
  )
  const detail = detailQuery.data
  const deliverables = detail?.deliverables || selectedSummary?.deliverables || { items: [] }

  if (!isTenantAdmin) {
    return (
      <div className="page">
        <div className="card">
          <h2 style={{ marginTop: 0 }}>成员内容工厂</h2>
          <div className="alert alert--error">仅公司 owner / 管理员可以查看成员项目。</div>
        </div>
      </div>
    )
  }

  return (
    <div className="page">
      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <div>
            <h2 style={{ margin: 0 }}>成员内容工厂</h2>
            <div className="small-muted" style={{ marginTop: 6 }}>只读查看成员项目、视频成果和剪辑发布指导。</div>
          </div>
          <button type="button" className="btn ghost" onClick={() => projectsQuery.refetch()}>
            {projectsQuery.isFetching ? '刷新中…' : '刷新'}
          </button>
        </div>

        <label className="small-muted" style={{ display: 'block', maxWidth: 360, marginTop: 16 }}>
          成员
          <select
            className="input"
            value={creatorUserId}
            onChange={(event) => {
              setCreatorUserId(event.target.value)
              setSelectedKey('')
            }}
          >
            <option value="">全部成员</option>
            {users.map((user) => (
              <option key={user.id} value={user.id}>
                {user.display_name || user.username || user.email}
                {user.usercode ? `（${user.usercode}）` : ''}
              </option>
            ))}
          </select>
        </label>

        {projectsQuery.isLoading && <Loading />}
        {!projectsQuery.isLoading && projects.length === 0 && (
          <div className="small-muted" style={{ marginTop: 18 }}>暂无成员内容项目。</div>
        )}

        {!projectsQuery.isLoading && projects.length > 0 && (
          <div className="table-wrapper" style={{ marginTop: 16, maxHeight: 430, overflow: 'auto' }}>
            <table className="table">
              <thead>
                <tr>
                  <th>项目</th>
                  <th style={{ width: 180 }}>创建人</th>
                  <th style={{ width: 150 }}>产品</th>
                  <th style={{ width: 100 }}>状态</th>
                  <th style={{ width: 130 }}>成果</th>
                  <th style={{ width: 170 }}>更新时间</th>
                  <th style={{ width: 80 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {projects.map((project) => (
                  <tr key={project.project_key}>
                    <td>{project.title || project.project_key}</td>
                    <td className="small-muted">{creatorLabel(project)}</td>
                    <td className="small-muted">{project.product_name || '-'}</td>
                    <td>
                      <span style={{ color: statusColor(project.status) }}>{project.status}</span>
                    </td>
                    <td className="small-muted">
                      {project.deliverables?.complete_count || 0} / {project.deliverables?.target_count || 0}
                    </td>
                    <td className="small-muted">
                      {project.updated_at ? new Date(project.updated_at).toLocaleString() : '-'}
                    </td>
                    <td>
                      <button type="button" className="btn ghost sm" onClick={() => setSelectedKey(project.project_key)}>查看</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {selectedKey && (
          <section style={{ borderTop: '1px solid var(--border)', marginTop: 20, paddingTop: 18 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
              <div>
                <h3 style={{ margin: 0 }}>{detail?.title || selectedSummary?.title || selectedKey}</h3>
                <div className="small-muted" style={{ marginTop: 5 }}>
                  {creatorLabel(selectedSummary || detail)} · {detail?.product_name || selectedSummary?.product_name || '-'}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <a className="btn ghost" href={adminContentFactoryDeliverablesZipUrl(wid, selectedKey, 'videos')}>批量下载视频</a>
                <a className="btn ghost" href={adminContentFactoryDeliverablesZipUrl(wid, selectedKey, 'guides')}>下载指导</a>
                <a className="btn" href={adminContentFactoryDeliverablesZipUrl(wid, selectedKey, 'all')}>下载全部成果</a>
              </div>
            </div>

            {detailQuery.isLoading && <Loading />}
            {!detailQuery.isLoading && (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12, marginTop: 16 }}>
                {(deliverables.items || []).map((item) => (
                  <article key={item.index} style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                      <strong>视频 {item.index}</strong>
                      <span className="small-muted">{item.status}</span>
                    </div>
                    {item.video ? (
                      <>
                        <video
                          src={adminContentFactoryAssetUrl(wid, selectedKey, item.video.id)}
                          controls
                          preload="metadata"
                          style={{ width: '100%', maxHeight: 300, marginTop: 10, background: '#000', borderRadius: 6 }}
                        />
                        <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                          <button
                            type="button"
                            className="btn ghost sm"
                            onClick={() => setPreview({
                              title: item.video.original_name || `视频 ${item.index}`,
                              url: adminContentFactoryAssetUrl(wid, selectedKey, item.video.id),
                            })}
                          >
                            放大预览
                          </button>
                          <a className="btn ghost sm" href={adminContentFactoryAssetUrl(wid, selectedKey, item.video.id)}>下载视频</a>
                          {item.guidance && (
                            <a className="btn ghost sm" href={adminContentFactoryAssetUrl(wid, selectedKey, item.guidance.id)}>剪辑指导</a>
                          )}
                        </div>
                      </>
                    ) : (
                      <div className="small-muted" style={{ marginTop: 12 }}>该序号尚无完整视频成果。</div>
                    )}
                  </article>
                ))}
              </div>
            )}
          </section>
        )}
      </div>

      {preview && (
        <Modal open title={preview.title} onClose={() => setPreview(null)}>
          <video src={preview.url} controls autoPlay style={{ width: '100%', maxHeight: '75vh', background: '#000', borderRadius: 8 }} />
        </Modal>
      )}
    </div>
  )
}
