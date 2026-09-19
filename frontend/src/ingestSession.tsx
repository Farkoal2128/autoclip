import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import {
  ApiError,
  api,
  type IngestActivityEvent,
  type JobSettingsOverrides,
  type RemoteIngestSession,
} from './api'

export type IngestKind = 'url' | 'file'

export type IngestLogEntry = {
  id: string
  time: string
  message: string
}

export type DownloadMetrics = {
  downloadedBytes: number | null
  totalBytes: number | null
  speedBytesS: number | null
  totalIsEstimate: boolean
}

type IngestSession = {
  busy: IngestKind | null
  error: Error | null
  entries: IngestLogEntry[]
  progress: number | null
  downloadMetrics: DownloadMetrics
  readyJobId: string | null
  startUrl: (url: string, overrides: JobSettingsOverrides) => Promise<void>
  startFile: (file: File, overrides: JobSettingsOverrides) => Promise<void>
  dismissError: () => void
  openReadyJob: () => void
  dismissReadyJob: () => void
}

const EMPTY_METRICS: DownloadMetrics = {
  downloadedBytes: null,
  totalBytes: null,
  speedBytesS: null,
  totalIsEstimate: false,
}

const IngestSessionContext = createContext<IngestSession | null>(null)

export function IngestSessionProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const location = useLocation()
  const locationRef = useRef(location)
  const localLogId = useRef(0)
  const [busy, setBusy] = useState<IngestKind | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [entries, setEntries] = useState<IngestLogEntry[]>([])
  const [progress, setProgress] = useState<number | null>(null)
  const [downloadMetrics, setDownloadMetrics] = useState<DownloadMetrics>(EMPTY_METRICS)
  const [readyJobId, setReadyJobId] = useState<string | null>(null)
  const [remoteSessionId, setRemoteSessionId] = useState<string | null>(null)
  const [terminalSessionId, setTerminalSessionId] = useState<string | null>(null)

  useEffect(() => {
    locationRef.current = location
  }, [location])

  const appendLocalLog = useCallback((message: string) => {
    setEntries((current) => {
      if (current[current.length - 1]?.message === message) return current
      localLogId.current += 1
      return [
        ...current,
        {
          id: `local:${localLogId.current}`,
          time: new Date().toLocaleTimeString(),
          message,
        },
      ].slice(-40)
    })
  }, [])

  const applyRemoteSession = useCallback(
    (session: RemoteIngestSession) => {
      setEntries(
        session.messages.map((entry, index) => ({
          id: `${session.id}:${index}`,
          time: new Date(entry.at).toLocaleTimeString(),
          message: entry.message,
        })),
      )
      setProgress(
        session.progress === null ? null : Math.max(0, Math.min(1, session.progress)),
      )
      setDownloadMetrics({
        downloadedBytes: session.downloaded_bytes,
        totalBytes: session.total_bytes,
        speedBytesS: session.speed_bytes_s,
        totalIsEstimate: session.total_is_estimate,
      })

      if (session.status === 'running') {
        setBusy('url')
        setError(null)
        setReadyJobId(null)
        setTerminalSessionId(null)
        return
      }

      setBusy(null)
      setRemoteSessionId(null)
      setTerminalSessionId(session.id)

      if (session.status === 'error') {
        setError(new ApiError(session.error || 'Remote ingest failed.', 422, session.hint))
        return
      }

      setProgress(1)
      setError(null)
      if (!session.job_id) {
        setError(new ApiError('The download finished without creating a processing job.', 500))
        return
      }

      if (locationRef.current.pathname === '/') {
        setTerminalSessionId(null)
        void api.clearRemoteIngest(session.id).catch(() => undefined)
        navigate(`/jobs/${session.job_id}`)
      } else {
        setReadyJobId(session.job_id)
      }
    },
    [navigate],
  )

  useEffect(() => {
    let cancelled = false

    void api
      .currentRemoteIngest()
      .then((session) => {
        if (cancelled || !session) return
        applyRemoteSession(session)
        if (session.status === 'running') setRemoteSessionId(session.id)
      })
      .catch(() => undefined)

    return () => {
      cancelled = true
    }
  }, [applyRemoteSession])

  useEffect(() => {
    if (!remoteSessionId) return

    let cancelled = false
    let timer: number | null = null

    const poll = async () => {
      try {
        const session = await api.currentRemoteIngest()
        if (cancelled) return

        if (!session || session.id !== remoteSessionId) {
          setRemoteSessionId(null)
          setBusy((current) => (current === 'url' ? null : current))
          return
        }

        applyRemoteSession(session)
        if (session.status === 'running') {
          timer = window.setTimeout(() => void poll(), 500)
        }
      } catch {
        if (!cancelled) timer = window.setTimeout(() => void poll(), 1000)
      }
    }

    timer = window.setTimeout(() => void poll(), 250)

    return () => {
      cancelled = true
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [applyRemoteSession, remoteSessionId])

  const startUrl = useCallback(
    async (url: string, overrides: JobSettingsOverrides) => {
      if (busy !== null) return

      setBusy('url')
      setError(null)
      setEntries([])
      setProgress(0)
      setDownloadMetrics(EMPTY_METRICS)
      setReadyJobId(null)
      setTerminalSessionId(null)

      try {
        const session = await api.startRemoteIngest(url, overrides)
        applyRemoteSession(session)
        if (session.status === 'running') setRemoteSessionId(session.id)
      } catch (err) {
        setBusy(null)
        setError(err instanceof Error ? err : new Error(String(err)))
      }
    },
    [applyRemoteSession, busy],
  )

  const onFileEvent = useCallback(
    (event: IngestActivityEvent) => {
      if (event.type === 'progress') {
        if (event.progress !== undefined && event.progress !== null) {
          setProgress(Math.max(0, Math.min(1, event.progress)))
        }
      } else if (event.message) {
        appendLocalLog(event.message)
      }
    },
    [appendLocalLog],
  )

  const startFile = useCallback(
    async (file: File, overrides: JobSettingsOverrides) => {
      if (busy !== null) return

      setBusy('file')
      setError(null)
      setEntries([])
      setProgress(0)
      setDownloadMetrics(EMPTY_METRICS)
      setReadyJobId(null)
      appendLocalLog('Preparing local upload')

      try {
        const source = await api.uploadSource(file, onFileEvent)
        setProgress(1)
        appendLocalLog('Source registered; creating processing job')
        const job = await api.createJob(source.id, overrides)
        appendLocalLog('Job queued; pipeline is ready')

        if (locationRef.current.pathname === '/') {
          navigate(`/jobs/${job.id}`)
        } else {
          setReadyJobId(job.id)
        }
      } catch (err) {
        appendLocalLog('Ingest stopped with an error')
        setError(err instanceof Error ? err : new Error(String(err)))
      } finally {
        setBusy(null)
      }
    },
    [appendLocalLog, busy, navigate, onFileEvent],
  )

  const clearTerminalSession = useCallback(() => {
    if (!terminalSessionId) return
    const sessionId = terminalSessionId
    setTerminalSessionId(null)
    void api.clearRemoteIngest(sessionId).catch(() => undefined)
  }, [terminalSessionId])

  const openReadyJob = useCallback(() => {
    if (!readyJobId) return
    const jobId = readyJobId
    setReadyJobId(null)
    clearTerminalSession()
    navigate(`/jobs/${jobId}`)
  }, [clearTerminalSession, navigate, readyJobId])

  const dismissReadyJob = useCallback(() => {
    setReadyJobId(null)
    clearTerminalSession()
  }, [clearTerminalSession])

  const dismissError = useCallback(() => {
    setError(null)
    clearTerminalSession()
  }, [clearTerminalSession])

  return (
    <IngestSessionContext.Provider
      value={{
        busy,
        error,
        entries,
        progress,
        downloadMetrics,
        readyJobId,
        startUrl,
        startFile,
        dismissError,
        openReadyJob,
        dismissReadyJob,
      }}
    >
      {children}
    </IngestSessionContext.Provider>
  )
}

export function useIngestSession(): IngestSession {
  const value = useContext(IngestSessionContext)
  if (!value) throw new Error('useIngestSession must be used inside IngestSessionProvider')
  return value
}
