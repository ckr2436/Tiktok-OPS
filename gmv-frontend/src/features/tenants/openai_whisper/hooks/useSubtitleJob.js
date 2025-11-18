// src/features/tenants/openai_whisper/hooks/useSubtitleJob.js
import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchSubtitleJob } from '../api/index.js'

const TERMINAL_STATES = new Set(['success', 'failed'])

export default function useSubtitleJob(wid) {
  const [jobId, setJobId] = useState(null)
  const [job, setJob] = useState(null)
  const [isPolling, setIsPolling] = useState(false)
  const pollTimer = useRef(null)

  const stopPolling = useCallback(() => {
    if (pollTimer.current) {
      clearTimeout(pollTimer.current)
      pollTimer.current = null
    }
    setIsPolling(false)
  }, [])

  const poll = useCallback(
    async (id) => {
      if (!wid || !id) return
      try {
        const data = await fetchSubtitleJob(wid, id)
        setJob(data)
        if (TERMINAL_STATES.has(String(data?.status || '').toLowerCase())) {
          stopPolling()
          return
        }
      } catch (err) {
        console.error('subtitle job poll failed', err)
        stopPolling()
        return
      }
      pollTimer.current = setTimeout(() => poll(id), 3000)
    },
    [stopPolling, wid],
  )

  const startPolling = useCallback(
    (id) => {
      if (!id) return
      setJobId(id)
      setIsPolling(true)
      poll(id)
    },
    [poll],
  )

  useEffect(() => {
    return () => {
      if (pollTimer.current) {
        clearTimeout(pollTimer.current)
      }
    }
  }, [])

  return {
    jobId,
    job,
    setJob,
    isPolling,
    startPolling,
    stopPolling,
    async refresh(targetId) {
      const id = targetId || jobId
      if (!wid || !id) return null
      const data = await fetchSubtitleJob(wid, id)
      setJob(data)
      return data
    },
  }
}

