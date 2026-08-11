// src/features/tenants/openai_whisper/pages/SubtitleRecognitionPage.jsx
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import FileDropZone from '../components/FileDropZone.jsx'
import SubtitleResult from '../components/SubtitleResult.jsx'
import SubtitleJobHistory from '../components/SubtitleJobHistory.jsx'
import ContactSheetResult from '../components/ContactSheetResult.jsx'
import DownloadVideoCard from '../components/DownloadVideoCard.jsx'
import useSubtitleJob from '../hooks/useSubtitleJob.js'
import {
  buildSubtitleDownloadUrl,
  clearSubtitleJobs,
  createSubtitleJob,
  deleteSubtitleJob,
  fetchSubtitleJobs,
  fetchLanguages,
  uploadSubtitleVideo,
} from '../api/index.js'

function normalizeShareLink(input) {
  const text = (input || '').trim()
  if (!text) return ''
  const match = text.match(/https?:\/\/[^\s]+/i)
  if (!match) return ''
  const candidate = match[0].replace(/[。．。,，]+$/, '')
  try {
    const url = new URL(candidate)
    return url.toString()
  } catch (err) {
    console.warn('invalid share url detected', candidate, err)
    return ''
  }
}

function toHistorySummary(jobData) {
  if (!jobData) return null
  return {
    job_id: jobData.job_id,
    filename: jobData.filename,
    status: jobData.status,
    error: jobData.error,
    translate: jobData.translate,
    show_bilingual: jobData.show_bilingual,
    source_language: jobData.source_language,
    detected_language: jobData.detected_language,
    target_language: jobData.target_language,
    translation_language: jobData.translation_language || jobData.target_language,
    do_subtitle: jobData.do_subtitle,
    do_contact_sheet: jobData.do_contact_sheet,
    do_download_only: jobData.do_download_only,
    subtitle_status: jobData.subtitle_status,
    contact_sheet_status: jobData.contact_sheet_status,
    download_status: jobData.download_status,
    created_at: jobData.created_at,
    updated_at: jobData.updated_at,
  }
}

export default function SubtitleRecognitionPage() {
  const { wid } = useParams()
  const [languages, setLanguages] = useState([])
  const [loading, setLoading] = useState(false)
  const [selectedFile, setSelectedFile] = useState(null)
  const [uploadedVideo, setUploadedVideo] = useState(null)
  const [shareLink, setShareLink] = useState('')
  const [sourceLanguage, setSourceLanguage] = useState('')
  const [translate, setTranslate] = useState(false)
  const [targetLanguage, setTargetLanguage] = useState('')
  const [showBilingual, setShowBilingual] = useState(false)
  const [doSubtitle, setDoSubtitle] = useState(true)
  const [doContactSheet, setDoContactSheet] = useState(false)
  const [contactInterval, setContactInterval] = useState('1')
  const [doDownloadOnly, setDoDownloadOnly] = useState(false)
  const [errorMessage, setErrorMessage] = useState('')
  const [uploadProgress, setUploadProgress] = useState(0)
  const [isUploading, setIsUploading] = useState(false)

  const { job, startPolling, setJob, stopPolling, refresh } = useSubtitleJob(wid)
  const [jobHistory, setJobHistory] = useState([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState('')
  const [selectedJobId, setSelectedJobId] = useState(null)
  const [isPasting, setIsPasting] = useState(false)
  const [deletingJobId, setDeletingJobId] = useState('')
  const [clearingHistory, setClearingHistory] = useState(false)
  const cleanedShareUrl = useMemo(() => normalizeShareLink(shareLink), [shareLink])

  const upsertHistory = useCallback((jobData) => {
    const summary = toHistorySummary(jobData)
    if (!summary) return
    setHistoryError('')
    setJobHistory((prev) => {
      const index = prev.findIndex((item) => item.job_id === summary.job_id)
      if (index >= 0) {
        const clone = [...prev]
        clone[index] = { ...clone[index], ...summary }
        return clone
      }
      return [summary, ...prev].slice(0, 20)
    })
  }, [])

  useEffect(() => {
    let mounted = true
    fetchLanguages(wid)
      .then((data) => {
        if (mounted) setLanguages(data)
      })
      .catch((err) => {
        console.error('load languages failed', err)
      })
    return () => {
      mounted = false
    }
  }, [wid])

  useEffect(() => {
    if (shareLink) {
      setSelectedFile(null)
      setUploadedVideo(null)
      setUploadProgress(0)
      setIsUploading(false)
    } else {
      setDoDownloadOnly(false)
    }
  }, [shareLink])

  const handlePasteShareLink = useCallback(async () => {
    if (!navigator?.clipboard?.readText) {
      setErrorMessage('当前浏览器不支持读取剪贴板，请手动粘贴链接。')
      return
    }
    try {
      setIsPasting(true)
      const text = await navigator.clipboard.readText()
      if (!text) {
        setErrorMessage('剪贴板为空，请复制短视频分享链接后重试。')
        return
      }
      setShareLink(text.trim())
      setErrorMessage('')
    } catch (err) {
      console.error('read clipboard failed', err)
      setErrorMessage('无法读取剪贴板，请检查浏览器权限或手动粘贴链接。')
    } finally {
      setIsPasting(false)
    }
  }, [])

  const refreshHistory = useCallback(async () => {
    if (!wid) return []
    setHistoryError('')
    setHistoryLoading(true)
    try {
      const jobs = await fetchSubtitleJobs(wid, { limit: 20 })
      setJobHistory(jobs)
      return jobs
    } catch (err) {
      console.error('load subtitle jobs failed', err)
      setHistoryError(err?.message || '加载任务列表失败，请稍后再试。')
      return []
    } finally {
      setHistoryLoading(false)
    }
  }, [wid])

  useEffect(() => {
    let cancelled = false
    async function bootstrapHistory() {
      const jobs = await refreshHistory()
      if (cancelled) return
      if (jobs.length > 0) {
        const latestId = jobs[0].job_id
        setSelectedJobId(latestId)
        await refresh(latestId)
      } else {
        setSelectedJobId(null)
        setJob(null)
        stopPolling()
      }
    }
    bootstrapHistory()
    return () => {
      cancelled = true
    }
  }, [refreshHistory, refresh, setJob, stopPolling])

  useEffect(() => {
    let cancelled = false
    setUploadedVideo(null)
    setUploadProgress(0)
    setIsUploading(false)
    if (!selectedFile) {
      return () => {
        cancelled = true
      }
    }
    async function performUpload() {
      setErrorMessage('')
      setIsUploading(true)
      try {
        const response = await uploadSubtitleVideo(wid, selectedFile, {
          onUploadProgress: (event) => {
            if (!event.total) return
            const percent = Math.round((event.loaded / event.total) * 100)
            if (!cancelled) setUploadProgress(percent)
          },
        })
        if (!cancelled) {
          setUploadedVideo(response)
          setUploadProgress(100)
        }
      } catch (err) {
        console.error('upload video failed', err)
        if (!cancelled) {
          setErrorMessage(err?.message || '上传视频失败，请稍后再试。')
          setUploadedVideo(null)
        }
      } finally {
        if (!cancelled) setIsUploading(false)
      }
    }
    performUpload()
    return () => {
      cancelled = true
    }
  }, [selectedFile, wid])

  useEffect(() => {
    if (job) {
      setSelectedJobId(job.job_id)
      upsertHistory(job)
    }
  }, [job, upsertHistory])

  const languageOptions = useMemo(() => languages ?? [], [languages])
  const contactIntervals = useMemo(() => ['0.5', '1', '1.5', '2'], [])

  const handleHistoryRefresh = useCallback(async () => {
    const jobs = await refreshHistory()
    if (!jobs.length) {
      setSelectedJobId(null)
      setJob(null)
      stopPolling()
      return
    }
    if (!jobs.some((item) => item.job_id === selectedJobId)) {
      const nextId = jobs[0].job_id
      setSelectedJobId(nextId)
      await refresh(nextId)
    }
  }, [refreshHistory, refresh, selectedJobId, setJob, stopPolling])

  const handleSelectHistoryJob = useCallback(
    async (jobItem) => {
      if (!jobItem?.job_id) return
      setSelectedJobId(jobItem.job_id)
      stopPolling()
      const detail = await refresh(jobItem.job_id)
      if (detail) {
        upsertHistory(detail)
        const terminal = new Set(['success', 'failed'])
        if (terminal.has(String(detail.status || '').toLowerCase())) stopPolling()
        else startPolling(detail.job_id)
      }
    },
    [refresh, startPolling, stopPolling, upsertHistory],
  )

  const handleDeleteJob = useCallback(
    async (jobItem, options = {}) => {
      if (!jobItem?.job_id || deletingJobId) return
      const active = ['pending', 'processing'].includes(String(jobItem.status || '').toLowerCase())
      const force = !!options.force || active
      const ok = window.confirm(
        force
          ? `确定强制删除任务「${jobItem.filename || jobItem.job_id}」吗？\n该任务当前仍显示处理中。强制删除会移除数据库记录和已生成文件，但不能中断可能已经开始的后台下载进程。`
          : `确定删除任务「${jobItem.filename || jobItem.job_id}」吗？\n该操作会删除数据库记录和对应生成文件，无法恢复。`,
      )
      if (!ok) return
      try {
        setDeletingJobId(jobItem.job_id)
        setHistoryError('')
        await deleteSubtitleJob(wid, jobItem.job_id, { force })
        setJobHistory((prev) => prev.filter((item) => item.job_id !== jobItem.job_id))
        if (selectedJobId === jobItem.job_id) {
          stopPolling()
          setSelectedJobId(null)
          setJob(null)
          const jobs = await refreshHistory()
          if (jobs.length) {
            setSelectedJobId(jobs[0].job_id)
            await refresh(jobs[0].job_id)
          }
        }
      } catch (err) {
        console.error('delete subtitle job failed', err)
        setHistoryError(err?.message || '删除任务失败，请稍后再试。')
      } finally {
        setDeletingJobId('')
      }
    },
    [deletingJobId, refresh, refreshHistory, selectedJobId, setJob, stopPolling, wid],
  )

  const handleClearJobs = useCallback(
    async (scope) => {
      if (clearingHistory) return
      const message = scope === 'failed'
        ? '确定清空所有失败任务吗？对应文件也会删除。'
        : '确定清空所有已完成任务吗？包括成功和失败任务，对应文件也会删除。'
      if (!window.confirm(message)) return
      try {
        setClearingHistory(true)
        setHistoryError('')
        const result = await clearSubtitleJobs(wid, { scope })
        const jobs = await refreshHistory()
        if (!jobs.some((item) => item.job_id === selectedJobId)) {
          stopPolling()
          setJob(null)
          if (jobs.length) {
            setSelectedJobId(jobs[0].job_id)
            await refresh(jobs[0].job_id)
          } else {
            setSelectedJobId(null)
          }
        }
        setHistoryError(result?.deleted ? `已清理 ${result.deleted} 条记录。` : '没有可清理的记录。')
      } catch (err) {
        console.error('clear subtitle jobs failed', err)
        setHistoryError(err?.message || '清空历史任务失败，请稍后再试。')
      } finally {
        setClearingHistory(false)
      }
    },
    [clearingHistory, refresh, refreshHistory, selectedJobId, setJob, stopPolling, wid],
  )

  async function handleSubmit(e) {
    e.preventDefault()
    setErrorMessage('')
    const trimmedLink = shareLink.trim()
    const hasValidShareLink = !!cleanedShareUrl
    const hasTask = doSubtitle || doContactSheet || (doDownloadOnly && hasValidShareLink)
    if (!hasValidShareLink && trimmedLink && doDownloadOnly) {
      setErrorMessage('分享链接格式不正确，请重新复制有效的短视频链接。')
      return
    }
    if (!hasValidShareLink && !uploadedVideo?.upload_id) {
      setErrorMessage('请先上传需要识别的视频文件，或粘贴有效的短视频分享链接。')
      return
    }
    if (!hasTask) {
      setErrorMessage('请至少勾选一种任务类型。')
      return
    }
    if (doDownloadOnly && !hasValidShareLink) {
      setErrorMessage('仅下载模式仅支持分享链接。')
      return
    }
    if (doContactSheet && !contactIntervals.includes(String(contactInterval))) {
      setErrorMessage('请选择合法的抽帧间隔。')
      return
    }
    if (doSubtitle && translate && !targetLanguage) {
      setErrorMessage('请选择翻译目标语言。')
      return
    }
    try {
      setLoading(true)
      const response = await createSubtitleJob(wid, {
        uploadId: hasValidShareLink ? null : uploadedVideo?.upload_id,
        shareUrl: hasValidShareLink ? cleanedShareUrl : null,
        sourceLanguage: sourceLanguage || null,
        translate: doSubtitle && translate,
        targetLanguage: doSubtitle ? targetLanguage || null : null,
        showBilingual: doSubtitle ? showBilingual : false,
        doSubtitle,
        doContactSheet,
        contactInterval: doContactSheet ? Number(contactInterval) : null,
        doDownloadOnly: doDownloadOnly && hasValidShareLink,
      })
      setJob(response)
      upsertHistory(response)
      setSelectedJobId(response.job_id)
      startPolling(response.job_id)
    } catch (err) {
      console.error('create subtitle job failed', err)
      setErrorMessage(err?.message || '提交任务失败，请稍后再试。')
    } finally {
      setLoading(false)
    }
  }

  const hasVideo = !!uploadedVideo || !!cleanedShareUrl
  const hasTaskSelection = doSubtitle || doContactSheet || (doDownloadOnly && !!cleanedShareUrl)
  const translationReady = !doSubtitle || !translate || targetLanguage
  const contactReady = !doContactSheet || contactIntervals.includes(String(contactInterval))
  const canSubmit = hasVideo && hasTaskSelection && translationReady && contactReady && !isUploading
  const showDownloads = job && job.do_subtitle && job.subtitle_status === 'success'
  const sourceDownloadUrl = job ? buildSubtitleDownloadUrl(wid, job.job_id, 'source') : null
  const translationDownloadUrl = job && job.translation_segments?.length ? buildSubtitleDownloadUrl(wid, job.job_id, 'translation') : null

  return (
    <div style={{ padding: 32 }}>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 28, margin: 0 }}>识别字幕</h1>
        <p style={{ color: '#6b7280', marginTop: 8 }}>上传视频，自动提取语音并生成字幕，可选择翻译语言并导出 SRT 文件。</p>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(320px, 420px) 1fr', gap: 24, alignItems: 'flex-start' }}>
        <form onSubmit={handleSubmit} style={{ background: '#fff', borderRadius: 16, border: '1px solid #e5e7eb', padding: 24, display: 'flex', flexDirection: 'column', gap: 20 }}>
          <div>
            <h2 style={{ fontSize: 18, margin: '0 0 12px' }}>上传视频</h2>
            <FileDropZone file={selectedFile} onFileChange={(file) => { setShareLink(''); setSelectedFile(file) }} disabled={loading || !!shareLink} uploadProgress={uploadProgress} isUploading={isUploading} />
          </div>
          <div style={{ border: '1px dashed #d1d5db', borderRadius: 12, padding: 16, background: '#f9fafb' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
              <div style={{ flex: 1 }}>
                <label style={{ display: 'block', fontWeight: 600, marginBottom: 8 }}>或粘贴短视频分享链接</label>
                <input type="text" value={shareLink} onChange={(e) => { setErrorMessage(''); setShareLink(e.target.value) }} placeholder="支持 TikTok、抖音、快手、YouTube、Facebook 的 HTTPS 公开链接" style={{ width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid #d1d5db' }} disabled={loading} />
              </div>
              <button type="button" onClick={handlePasteShareLink} disabled={loading || isPasting} style={{ whiteSpace: 'nowrap', padding: '10px 16px', borderRadius: 10, border: '1px solid #2563eb', background: '#2563eb', color: '#fff', fontWeight: 600, cursor: loading ? 'not-allowed' : 'pointer', height: 44, alignSelf: 'flex-end' }}>{isPasting ? '读取中…' : '一键粘贴'}</button>
            </div>
            <p style={{ color: '#6b7280', marginTop: 8, marginBottom: 0 }}>提交后将在后台使用 yt-dlp 下载并识别视频，如链接需登录授权会在结果中提示。</p>
          </div>
          <div style={{ border: '1px solid #e5e7eb', borderRadius: 12, padding: 16, background: '#f9fafb', display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ fontWeight: 600 }}>任务类型</div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}><input type="checkbox" checked={doSubtitle} onChange={(e) => { setDoSubtitle(e.target.checked); if (!e.target.checked) { setTranslate(false); setTargetLanguage(''); setShowBilingual(false) } }} disabled={loading} /><span>识别字幕</span></label>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}><input type="checkbox" checked={doContactSheet} onChange={(e) => setDoContactSheet(e.target.checked)} disabled={loading} /><span>拆解视频图片（生成 contact sheet）</span></label>
              {doContactSheet ? (
                <div style={{ marginLeft: 24 }}>
                  <label style={{ display: 'block', marginBottom: 6 }}>抽帧间隔</label>
                  <select value={contactInterval} onChange={(e) => setContactInterval(e.target.value)} disabled={loading} style={{ width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid #d1d5db' }}>{contactIntervals.map((opt) => <option key={opt} value={opt}>每 {opt} 秒</option>)}</select>
                </div>
              ) : null}
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, opacity: shareLink ? 1 : 0.6 }}><input type="checkbox" checked={doDownloadOnly} onChange={(e) => setDoDownloadOnly(e.target.checked)} disabled={loading || !shareLink} /><span>下载源视频文件（仅分享链接可用）</span></label>
          </div>
          <div>
            <label style={{ display: 'block', fontWeight: 600, marginBottom: 8 }}>原视频语言（可选）</label>
            <select value={sourceLanguage} onChange={(e) => setSourceLanguage(e.target.value)} style={{ width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid #d1d5db' }} disabled={loading}>
              <option value="">自动检测</option>
              {languageOptions.map((lang) => <option key={lang.code} value={lang.code}>{lang.name}</option>)}
            </select>
          </div>
          <div style={{ border: '1px solid #e5e7eb', borderRadius: 12, padding: 16, background: '#f9fafb' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}><input type="checkbox" checked={translate} onChange={(e) => { setTranslate(e.target.checked); if (!e.target.checked) { setTargetLanguage(''); setShowBilingual(false) } }} disabled={loading || !doSubtitle} /><span style={{ fontWeight: 600 }}>需要翻译</span></label>
            {translate ? (
              <div style={{ marginTop: 12 }}>
                <label style={{ display: 'block', marginBottom: 8 }}>目标语言</label>
                <select value={targetLanguage} onChange={(e) => setTargetLanguage(e.target.value)} style={{ width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid #d1d5db' }} disabled={loading || !doSubtitle}>
                  <option value="">请选择</option>
                  {languageOptions.map((lang) => <option key={lang.code} value={lang.code}>{lang.name}</option>)}
                </select>
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}><input type="checkbox" checked={showBilingual} onChange={(e) => setShowBilingual(e.target.checked)} disabled={loading || !doSubtitle} /><span>同时显示原文与译文</span></label>
              </div>
            ) : null}
          </div>
          {errorMessage ? <div style={{ color: '#b91c1c', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 10, padding: '10px 12px' }}>{errorMessage}</div> : null}
          <button type="submit" disabled={!canSubmit || loading} style={{ border: 'none', borderRadius: 999, padding: '12px 20px', fontSize: 16, fontWeight: 600, background: canSubmit ? '#2563eb' : '#93c5fd', color: '#fff', cursor: !canSubmit || loading ? 'not-allowed' : 'pointer' }}>{isUploading ? `上传中…${uploadProgress ? ` ${uploadProgress}%` : ''}` : loading ? '正在创建任务…' : '开始任务'}</button>
        </form>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <SubtitleResult job={job} />
          <ContactSheetResult job={job} />
          <DownloadVideoCard job={job} />
          {showDownloads ? (
            <div style={{ background: '#fff', borderRadius: 16, border: '1px solid #e5e7eb', padding: 20 }}>
              <div style={{ fontWeight: 600, marginBottom: 12 }}>导出字幕</div>
              <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                <a href={sourceDownloadUrl} target="_blank" rel="noreferrer" style={{ padding: '10px 16px', borderRadius: 10, border: '1px solid #d1d5db', textDecoration: 'none', color: '#111827', background: '#f9fafb' }}>下载原语言字幕</a>
                {translationDownloadUrl ? <a href={translationDownloadUrl} target="_blank" rel="noreferrer" style={{ padding: '10px 16px', borderRadius: 10, border: '1px solid #d1d5db', textDecoration: 'none', color: '#111827', background: '#eef2ff' }}>下载翻译字幕</a> : null}
              </div>
            </div>
          ) : null}
          <SubtitleJobHistory jobs={jobHistory} selectedJobId={selectedJobId} onSelect={handleSelectHistoryJob} onRefresh={handleHistoryRefresh} onDelete={handleDeleteJob} onClear={handleClearJobs} loading={historyLoading} deletingJobId={deletingJobId} clearing={clearingHistory} errorMessage={historyError} />
        </div>
      </div>
    </div>
  )
}
