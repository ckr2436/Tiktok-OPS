// src/features/tenants/openai_whisper/components/FileDropZone.jsx
import { useRef, useState } from 'react'

export default function FileDropZone({
  file,
  onFileChange,
  disabled,
  uploadProgress,
  isUploading,
}) {
  const inputRef = useRef(null)
  const [isDragging, setIsDragging] = useState(false)

  function handleFiles(selected) {
    const picked = selected?.[0]
    if (picked && typeof onFileChange === 'function') {
      onFileChange(picked)
    }
  }

  function handleDrop(event) {
    event.preventDefault()
    event.stopPropagation()
    setIsDragging(false)
    if (disabled) return
    if (event.dataTransfer?.files?.length) {
      handleFiles(event.dataTransfer.files)
    }
  }

  function handleDrag(event) {
    event.preventDefault()
    event.stopPropagation()
    if (disabled) return
    if (event.type === 'dragenter' || event.type === 'dragover') {
      setIsDragging(true)
    } else if (event.type === 'dragleave') {
      setIsDragging(false)
    }
  }

  return (
    <div
      onDrop={handleDrop}
      onDragOver={handleDrag}
      onDragEnter={handleDrag}
      onDragLeave={handleDrag}
      style={{
        border: '2px dashed #9ca3af',
        borderRadius: 12,
        padding: 32,
        textAlign: 'center',
        background: isDragging ? '#eef2ff' : '#f9fafb',
        cursor: disabled ? 'not-allowed' : 'pointer',
        transition: 'background 0.2s ease',
      }}
      onClick={() => {
        if (disabled) return
        inputRef.current?.click()
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept="video/*"
        style={{ display: 'none' }}
        onChange={(e) => handleFiles(e.target.files)}
        disabled={disabled}
      />
      {file ? (
        <div>
          <p style={{ fontSize: 18, fontWeight: 600 }}>{file.name}</p>
          <p style={{ color: '#6b7280', marginTop: 4 }}>大小：{(file.size / (1024 * 1024)).toFixed(2)} MB</p>

          {typeof uploadProgress === 'number' && uploadProgress > 0 ? (
            <div style={{ marginTop: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                <span style={{ color: '#4b5563', fontSize: 14 }}>
                  {isUploading ? '上传中…' : uploadProgress >= 100 ? '上传完成' : '准备上传'}
                </span>
                <span style={{ color: '#111827', fontSize: 14 }}>{uploadProgress}%</span>
              </div>
              <div
                style={{
                  width: '100%',
                  height: 8,
                  borderRadius: 999,
                  background: '#e5e7eb',
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    width: `${Math.min(uploadProgress, 100)}%`,
                    height: '100%',
                    borderRadius: 999,
                    background: '#2563eb',
                    transition: 'width 0.2s ease',
                  }}
                />
              </div>
            </div>
          ) : null}

          <button
            type="button"
            style={{
              marginTop: 12,
              background: '#fee2e2',
              border: '1px solid #fecaca',
              borderRadius: 8,
              padding: '6px 16px',
              color: '#b91c1c',
              cursor: disabled ? 'not-allowed' : 'pointer',
            }}
            onClick={(e) => {
              e.stopPropagation()
              if (disabled) return
              if (typeof onFileChange === 'function') {
                onFileChange(null)
              }
            }}
          >
            重新选择
          </button>
        </div>
      ) : (
        <div>
          <p style={{ fontSize: 18, fontWeight: 600 }}>点击或拖拽视频到这里上传</p>
          <p style={{ color: '#6b7280', marginTop: 4 }}>支持 MP4、MOV、WEBM 等常见格式</p>
        </div>
      )}
    </div>
  )
}

