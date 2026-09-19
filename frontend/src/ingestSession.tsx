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
  api,
  type IngestActivityEvent,
  type JobSettingsOverrides,
  type Source,
} from './api'

export type IngestKind = 'url' | 'file'

export type IngestLogEntry = {
  id: number
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
  const logId = useRef(0)
  const [busy, setBusy] = useState<IngestKind | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [entries, setEntries] = useState<IngestLogEntry[]>([])
  const [progress, setProgress] = useState<number | null>(null)
  const [downloadMetrics, setDownloadMetrics] = useState<DownloadMetrics>(EMPTY_METRICS)
  const [readyJobId, setReadyJobId] = useState<string | null>(null)

  useEffect(() => {
    locationRef.current = location
  }, [location])

  const appendLog = useCallback((message: string) => {
    setEntries((current) => {
      if (current[current.length - 1]?.message === message) return current
      logId.current += 1
      return [
        ...current,
        { id: logId.current, time: new Date().toLocaleTimeString(), message },
      ].slice(-40)
    })
  }, [])

  const onEvent = useCallback(
    (event: IngestActivityEvent) => {
      if (event.type === 'progress') {
        if (event.progress !== undefined && event.progress !== null) {
          setProgress(Math.max(0, Math.min(1, event.progress)))
        }
        if (
          event.downloadedBytes !== undefined ||
          event.totalBytes !== undefined ||
          event.speedBytesS !== undefined
        ) {
          setDownloadMetrics((current) => ({
            downloadedBytes: event.downloadedBytes ?? current.downloadedBytes,
            totalBytes: event.totalBytes ?? current.totalBytes,
            speedBytesS: event.speedBytesS ?? current.speedBytesS,
            totalIsEstimate: event.totalIsEstimate ?? current.totalIsEstimate,
          }))
        }
      } else if (event.message) {
        appendLog(event.message)
      }
    },
    [appendLog],
  )

  const start = useCallback(
    async (
      kind: IngestKind,
      run: (onEvent: (event: IngestActivityEvent) => void) => Promise<Source>,
      overrides: JobSettingsOverrides,
    ) => {
      if (busy !== null) return

      setBusy(kind)
      setError(null)
      setEntries([])
      setProgress(0)
      setDownloadMetrics(EMPTY_METRICS)
      setReadyJobId(null)
      appendLog(kind === 'url' ? 'Starting remote video fetch' : 'Preparing local upload')

      try {
        const source = await run(onEvent)
        setProgress(1)
        appendLog('Source registered; creating processing job')
        const job = await api.createJob(source.id, overrides)
        appendLog('Job queued; pipeline is ready')

        if (locationRef.current.pathname === '/') {
          navigate(`/jobs/${job.id}`)
        } else {
          setReadyJobId(job.id)
        }
      } catch (err) {
        appendLog('Ingest stopped with an error')
        setError(err instanceof Error ? err : new Error(String(err)))
      } finally {
        setBusy(null)
      }
    },
    [appendLog, busy, navigate, onEvent],
  )

  const startUrl = useCallback(
    (url: string, overrides: JobSettingsOverrides) =>
      start('url', (handler) => api.ingestUrl(url, undefined, handler), overrides),
    [start],
  )

  const startFile = useCallback(
    (file: File, overrides: JobSettingsOverrides) =>
      start('file', (handler) => api.uploadSource(file, handler), overrides),
    [start],
  )

  const openReadyJob = useCallback(() => {
    if (!readyJobId) return
    const jobId = readyJobId
    setReadyJobId(null)
    navigate(`/jobs/${jobId}`)
  }, [navigate, readyJobId])

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
        dismissError: () => setError(null),
        openReadyJob,
        dismissReadyJob: () => setReadyJobId(null),
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
