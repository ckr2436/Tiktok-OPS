// src/features/tenants/openai_whisper/pages/SubtitleRecognitionPage.jsx
import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import FileDropZone from '../components/FileDropZone.jsx'
import SubtitleResult from '../components/SubtitleResult.jsx'
import useSubtitleJob from '../hooks/useSubtitleJob.js'
import {
  buildSubtitleDownloadUrl,
  createSubtitleJob,
  fetchLanguages,
  uploadSubtitleVideo,
} from '../api/index.js'

export default function SubtitleRecognitionPage() {
  const { wid } = useParams()
  const [languages, setLanguages] = useState([])
  const [loading, setLoading] = useState(false)
  const [selectedFile, setSelectedFile] = useState(null)
  const [uploadedVideo, setUploadedVideo] = useState(null)
  const [sourceLanguage, setSourceLanguage] = useState('')
  const [translate, setTranslate] = useState(false)
  const [targetLanguage, setTargetLanguage] = useState('')
  const [showBilingual, setShowBilingual] = useState(false)
  const [errorMessage, setErrorMessage] = useState('')
  const [uploadProgress, setUploadProgress] = useState(0)
  const [isUploading, setIsUploading] = useState(false)

  const { job, startPolling, setJob } = useSubtitleJob(wid)

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
            if (!cancelled) {
              setUploadProgress(percent)
            }
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
        if (!cancelled) {
          setIsUploading(false)
        }
      }
    }

    performUpload()

    return () => {
      cancelled = true
    }
  }, [selectedFile, wid])

  const languageOptions = useMemo(() => languages ?? [], [languages])

  async function handleSubmit(e) {
    e.preventDefault()
    setErrorMessage('')
    if (!uploadedVideo?.upload_id) {
      setErrorMessage('请先上传需要识别的视频文件。')
      return
    }
    if (translate && !targetLanguage) {
      setErrorMessage('请选择翻译目标语言。')
      return
    }
    try {
      setLoading(true)
      const response = await createSubtitleJob(wid, {
        uploadId: uploadedVideo.upload_id,
        sourceLanguage: sourceLanguage || null,
        translate,
        targetLanguage: targetLanguage || null,
        showBilingual,
      })
      setJob(response)
      startPolling(response.job_id)
    } catch (err) {
      console.error('create subtitle job failed', err)
      setErrorMessage(err?.message || '提交任务失败，请稍后再试。')
    } finally {
      setLoading(false)
    }
  }

  const canSubmit = !!uploadedVideo && (!translate || targetLanguage) && !isUploading
  const showDownloads = job && job.status === 'success'
  const sourceDownloadUrl = job
    ? buildSubtitleDownloadUrl(wid, job.job_id, 'source')
    : null
  const translationDownloadUrl =
    job && job.translation_segments?.length
      ? buildSubtitleDownloadUrl(wid, job.job_id, 'translation')
      : null

  return (
    <div style={{ padding: 32 }}>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 28, margin: 0 }}>识别字幕</h1>
        <p style={{ color: '#6b7280', marginTop: 8 }}>
          上传视频，自动提取语音并生成字幕，可选择翻译语言并导出 SRT 文件。
        </p>
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(320px, 420px) 1fr',
          gap: 24,
          alignItems: 'flex-start',
        }}
      >
        <form
          onSubmit={handleSubmit}
          style={{
            background: '#fff',
            borderRadius: 16,
            border: '1px solid #e5e7eb',
            padding: 24,
            display: 'flex',
            flexDirection: 'column',
            gap: 20,
          }}
        >
          <div>
            <h2 style={{ fontSize: 18, margin: '0 0 12px' }}>上传视频</h2>
            <FileDropZone
              file={selectedFile}
              onFileChange={setSelectedFile}
              disabled={loading}
              uploadProgress={uploadProgress}
              isUploading={isUploading}
            />
          </div>

          <div>
            <label style={{ display: 'block', fontWeight: 600, marginBottom: 8 }}>
              原视频语言（可选）
            </label>
            <select
              value={sourceLanguage}
              onChange={(e) => setSourceLanguage(e.target.value)}
              style={{
                width: '100%',
                padding: '10px 12px',
                borderRadius: 10,
                border: '1px solid #d1d5db',
              }}
              disabled={loading}
            >
              <option value="">自动检测</option>
              {languageOptions.map((lang) => (
                <option key={lang.code} value={lang.code}>
                  {lang.name}
                </option>
              ))}
            </select>
          </div>

          <div
            style={{
              border: '1px solid #e5e7eb',
              borderRadius: 12,
              padding: 16,
              background: '#f9fafb',
            }}
          >
            <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input
                type="checkbox"
                checked={translate}
                onChange={(e) => {
                  setTranslate(e.target.checked)
                  if (!e.target.checked) {
                    setTargetLanguage('')
                    setShowBilingual(false)
                  }
                }}
                disabled={loading}
              />
              <span style={{ fontWeight: 600 }}>需要翻译</span>
            </label>

            {translate ? (
              <div style={{ marginTop: 12 }}>
                <label style={{ display: 'block', marginBottom: 8 }}>
                  目标语言
                </label>
                <select
                  value={targetLanguage}
                  onChange={(e) => setTargetLanguage(e.target.value)}
                  style={{
                    width: '100%',
                    padding: '10px 12px',
                    borderRadius: 10,
                    border: '1px solid #d1d5db',
                  }}
                  disabled={loading}
                >
                  <option value="">请选择</option>
                  {languageOptions.map((lang) => (
                    <option key={lang.code} value={lang.code}>
                      {lang.name}
                    </option>
                  ))}
                </select>

                <label
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    marginTop: 12,
                  }}
                >
                  <input
                    type="checkbox"
                    checked={showBilingual}
                    onChange={(e) => setShowBilingual(e.target.checked)}
                    disabled={loading}
                  />
                  <span>同时显示原文与译文</span>
                </label>
              </div>
            ) : null}
          </div>

          {errorMessage ? (
            <div
              style={{
                color: '#b91c1c',
                background: '#fef2f2',
                border: '1px solid #fecaca',
                borderRadius: 10,
                padding: '10px 12px',
              }}
            >
              {errorMessage}
            </div>
          ) : null}

          <button
            type="submit"
            disabled={!canSubmit || loading}
            style={{
              border: 'none',
              borderRadius: 999,
              padding: '12px 20px',
              fontSize: 16,
              fontWeight: 600,
              background: canSubmit ? '#2563eb' : '#93c5fd',
              color: '#fff',
              cursor: !canSubmit || loading ? 'not-allowed' : 'pointer',
            }}
          >
            {isUploading
              ? `上传中…${uploadProgress ? ` ${uploadProgress}%` : ''}`
              : loading
                ? '正在创建任务…'
                : '开始识别'}
          </button>
        </form>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <SubtitleResult job={job} />

          {showDownloads ? (
            <div
              style={{
                background: '#fff',
                borderRadius: 16,
                border: '1px solid #e5e7eb',
                padding: 20,
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 12 }}>导出字幕</div>
              <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                <a
                  href={sourceDownloadUrl}
                  target="_blank"
                  rel="noreferrer"
                  style={{
                    padding: '10px 16px',
                    borderRadius: 10,
                    border: '1px solid #d1d5db',
                    textDecoration: 'none',
                    color: '#111827',
                    background: '#f9fafb',
                  }}
                >
                  下载原语言字幕
                </a>
                {translationDownloadUrl ? (
                  <a
                    href={translationDownloadUrl}
                    target="_blank"
                    rel="noreferrer"
                    style={{
                      padding: '10px 16px',
                      borderRadius: 10,
                      border: '1px solid #d1d5db',
                      textDecoration: 'none',
                      color: '#111827',
                      background: '#eef2ff',
                    }}
                  >
                    下载翻译字幕
                  </a>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}

