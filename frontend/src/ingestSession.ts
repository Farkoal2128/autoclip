import { useSyncExternalStore } from 'react'

import {
  api,
  type IngestActivityEvent,
  type Job,
  type JobSettingsOverrides,
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

export type IngestSessionState = {
  active: boolean
  kind: IngestKind | null
  progress: number | null
  downloadMetrics: DownloadMetrics
  entries: IngestLogEntry[]
  error: Error | null
  jobId: string | null
}

const emptyMetrics = (): DownloadMetrics => ({
  downloadedBytes: null,
  totalBytes: null,
  speedBytesS: null,
  totalIsEstimate: false,
})

let logId = 0
let state: IngestSessionState = {
  active: false,
  kind: null,
  progress: null,
  downloadMetrics: emptyMetrics(),
  entries: [],
  error: null,
  jobId: null,
}

const listeners = new Set<() => void>()

function emit(next: IngestSessionState) {
  state = next
  for (const listener of listeners) listener()
}

function patch(update: Partial<IngestSessionState>) {
  emit({ ...state, ...update })
}

function append(message: string) {
  if (state.entries[state.entries.length - 1]?.message === message) return
  logId += 1
  patch({
    entries: [
      ...state.entries,
      { id: logId, time: new Date().toLocaleTimeString(), message },
    ].slice(-40),
  })
}

function onEvent(event: IngestActivityEvent) {
  if (event.type === 'progress') {
    const progress =
      event.progress === undefined || event.progress === null
        ? state.progress
        : Math.max(0, Math.min(1, event.progress))

    patch({
      progress,
      downloadMetrics: {
        downloadedBytes:
          event.downloadedBytes ?? state.downloadMetrics.downloadedBytes,
        totalBytes: event.totalBytes ?? state.downloadMetrics.totalBytes,
        speedBytesS: event.speedBytesS ?? state.downloadMetrics.speedBytesS,
        totalIsEstimate:
          event.totalIsEstimate ?? state.downloadMetrics.totalIsEstimate,
      },
    })
    return
  }

  if (event.message) append(event.message)
}

async function run(
  kind: IngestKind,
  ingest: (onActivity: (event: IngestActivityEvent) => void) => Promise<{ id: string }>,
  overrides: JobSettingsOverrides,
): Promise<Job> {
  if (state.active) {
    throw new Error('Another source is already being ingested.')
  }

  emit({
    active: true,
    kind,
    progress: 0,
    downloadMetrics: emptyMetrics(),
    entries: [],
    error: null,
    jobId: null,
  })
  append(kind === 'url' ? 'Starting remote video fetch' : 'Preparing local upload')

  try {
    const source = await ingest(onEvent)
    patch({ progress: 1 })
    append('Source registered; creating processing job')
    const job = await api.createJob(source.id, overrides)
    append('Job queued; opening pipeline progress')
    patch({ active: false, jobId: job.id })
    return job
  } catch (error) {
    append('Ingest stopped with an error')
    patch({
      active: false,
      error: error instanceof Error ? error : new Error(String(error)),
    })
    throw error
  }
}

export function startRemoteIngest(
  url: string,
  overrides: JobSettingsOverrides,
): Promise<Job> {
  return run(
    'url',
    (onActivity) => api.ingestUrl(url, undefined, onActivity),
    overrides,
  )
}

export function startFileIngest(
  file: File,
  overrides: JobSettingsOverrides,
): Promise<Job> {
  return run('file', (onActivity) => api.uploadSource(file, onActivity), overrides)
}

export function dismissIngestError() {
  if (state.error) patch({ error: null })
}

export function useIngestSession(): IngestSessionState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => state,
    () => state,
  )
}
