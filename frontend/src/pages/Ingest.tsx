import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import {
  ApiError,
  api,
  formatBytes,
  formatDuration,
  type IngestActivityEvent,
  type Job,
  type JobSettingsOverrides,
  type ProviderStatus,
} from '../api'
import { ErrorNote } from '../components/ErrorNote'

export function Ingest() {
  const navigate = useNavigate()
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState<'url' | 'file' | null>(null)
  const [error, setError] = useState<ApiError | Error | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [providers, setProviders] = useState<ProviderStatus[]>([])
  const [ingestLog, setIngestLog] = useState<IngestLogEntry[]>([])
  const [ingestProgress, setIngestProgress] = useState<number | null>(null)
  const [downloadMetrics, setDownloadMetrics] = useState<DownloadMetrics>({
    downloadedBytes: null,
    totalBytes: null,
    speedBytesS: null,
    totalIsEstimate: false,
  })
  const [removingJobId, setRemovingJobId] = useState<string | null>(null)
  const [overrides, setOverrides] = useState<JobSettingsOverrides>({})
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [dragging, setDragging] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const ingestLogId = useRef(0)

  useEffect(() => {
    api.listJobs(8).then(setJobs).catch(() => undefined)
    api.providerStatus().then(setProviders).catch(() => undefined)
  }, [])

  const appendIngestLog = useCallback((message: string) => {
    setIngestLog((current) => {
      if (current[current.length - 1]?.message === message) return current
      ingestLogId.current += 1
      return [
        ...current,
        {
          id: ingestLogId.current,
          time: new Date().toLocaleTimeString(),
          message,
        },
      ].slice(-40)
    })
  }, [])

  const onIngestEvent = useCallback(
    (event: IngestActivityEvent) => {
      if (event.type === 'progress') {
        if (event.progress !== undefined && event.progress !== null) {
          setIngestProgress(Math.max(0, Math.min(1, event.progress)))
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
        appendIngestLog(event.message)
      }
    },
    [appendIngestLog],
  )

  const start = useCallback(
    async (
      kind: 'url' | 'file',
      run: (onEvent: (event: IngestActivityEvent) => void) => Promise<{ id: string }>,
    ) => {
      setBusy(kind)
      setError(null)
      setIngestLog([])
      setIngestProgress(0)
      setDownloadMetrics({
        downloadedBytes: null,
        totalBytes: null,
        speedBytesS: null,
        totalIsEstimate: false,
      })
      appendIngestLog(kind === 'url' ? 'Starting remote video fetch' : 'Preparing local upload')
      try {
        const source = await run(onIngestEvent)
        setIngestProgress(1)
        appendIngestLog('Source registered; creating processing job')
        const job = await api.createJob(source.id, overrides)
        appendIngestLog('Job queued; opening pipeline progress')
        navigate(`/jobs/${job.id}`)
      } catch (err) {
        appendIngestLog('Ingest stopped with an error')
        setError(err as Error)
      } finally {
        setBusy(null)
      }
    },
    [appendIngestLog, navigate, onIngestEvent, overrides],
  )

  const submitUrl = (event: React.FormEvent) => {
    event.preventDefault()
    if (!url.trim()) return
    void start('url', (onEvent) => api.ingestUrl(url.trim(), undefined, onEvent))
  }

  const submitFile = (file: File) =>
    void start('file', (onEvent) => api.uploadSource(file, onEvent))

  const removeJob = async (job: Job) => {
    if (
      !window.confirm(
        `Remove "${job.source?.title || 'Untitled'}"? This deletes its AutoClip project files and cannot be undone.`,
      )
    ) {
      return
    }

    setRemovingJobId(job.id)
    setError(null)
    try {
      await api.deleteJob(job.id)
      setJobs((current) => current.filter((item) => item.id !== job.id))
    } catch (err) {
      setError(err as Error)
    } finally {
      setRemovingJobId(null)
    }
  }

  const usableProvider = providers.find((p) => p.available)

  return (
    <div className="pt-14">
      {/* Masthead. Left-aligned and asymmetric — the field is the subject, not
          a centred hero card. */}
      <div className="rise max-w-3xl">
        <p className="eyebrow">Local · No accounts · No watermarks</p>
        <h1 className="mt-5 font-display text-[clamp(2.75rem,7vw,5.5rem)] leading-[0.95] text-ink-100">
          Long video in.
          <br />
          <span className="italic text-sodium-500">Shorts</span> out.
        </h1>
      </div>

      <div className="mt-16 grid gap-x-16 gap-y-12 lg:grid-cols-[1.35fr_1fr]">
        {/* URL */}
        <section className="rise" style={{ animationDelay: '90ms' }}>
          <form onSubmit={submitUrl}>
            <label htmlFor="url" className="eyebrow">
              Paste a link
            </label>
            <div className="mt-3 flex items-end gap-4">
              <input
                id="url"
                className="field font-display text-xl md:text-2xl"
                placeholder="YouTube URL or twitch.tv/videos/…"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                autoComplete="off"
                spellCheck={false}
                disabled={busy !== null}
              />
              <button
                type="submit"
                className="btn btn-primary shrink-0"
                disabled={busy !== null || !url.trim()}
              >
                {busy === 'url' ? 'Fetching…' : 'Start'}
              </button>
            </div>
          </form>

          <p className="mt-3 text-xs leading-relaxed text-ink-500">
            YouTube videos and Twitch VODs are supported. Only download video you own or have
            the rights to process.
          </p>

          <AdvancedOptions
            open={advancedOpen}
            onToggle={() => setAdvancedOpen((v) => !v)}
            overrides={overrides}
            onChange={setOverrides}
            providers={providers}
          />
        </section>

        {/* Upload */}
        <section className="rise" style={{ animationDelay: '160ms' }}>
          <p className="eyebrow">Or drop a file</p>
          <div
            onDragOver={(e) => {
              e.preventDefault()
              setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault()
              setDragging(false)
              const file = e.dataTransfer.files[0]
              if (file) submitFile(file)
            }}
            className={[
              'mt-3 flex min-h-52 cursor-pointer flex-col items-center justify-center gap-2 border border-dashed px-6 text-center transition-colors duration-200',
              dragging
                ? 'border-sodium-500 bg-sodium-700/10'
                : 'border-ink-700 hover:border-ink-600',
            ].join(' ')}
            onClick={() => fileInput.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') fileInput.current?.click()
            }}
          >
            <span className="font-display text-2xl text-ink-200">
              {busy === 'file' ? 'Uploading…' : 'Drop video or audio'}
            </span>
            <span className="text-xs text-ink-500">mp4 · mov · mkv · webm · mp3 · wav · m4a</span>
          </div>
          <input
            ref={fileInput}
            type="file"
            className="hidden"
            accept="video/*,audio/*"
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) submitFile(file)
              e.target.value = ''
            }}
          />
        </section>
      </div>

      {(busy !== null || ingestLog.length > 0) && (
        <IngestActivityPanel
          entries={ingestLog}
          progress={ingestProgress}
          active={busy !== null}
          remoteDownload={busy === 'url' || downloadMetrics.totalBytes !== null}
          downloadMetrics={downloadMetrics}
        />
      )}

      {error && (
        <div className="mt-10 max-w-3xl">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {providers.length > 0 && !usableProvider && (
        <p className="mt-10 max-w-3xl border-l-2 border-sodium-600 pl-4 text-sm text-ink-300">
          No AI provider is reachable yet, so clip selection will fail. Add an API key or
          start Ollama in{' '}
          <a href="/settings" className="text-sodium-500 underline underline-offset-4">
            Settings
          </a>
          .
        </p>
      )}

      <RecentJobs jobs={jobs} removingJobId={removingJobId} onRemove={removeJob} />
    </div>
  )
}

type IngestLogEntry = {
  id: number
  time: string
  message: string
}

type DownloadMetrics = {
  downloadedBytes: number | null
  totalBytes: number | null
  speedBytesS: number | null
  totalIsEstimate: boolean
}

function IngestActivityPanel({
  entries,
  progress,
  active,
  remoteDownload,
  downloadMetrics,
}: {
  entries: IngestLogEntry[]
  progress: number | null
  active: boolean
  remoteDownload: boolean
  downloadMetrics: DownloadMetrics
}) {
  const percent = progress === null ? null : Math.round(progress * 100)

  return (
    <section className="mt-8 max-w-3xl border border-ink-800 bg-ink-850/35 p-4">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="eyebrow">Ingest activity</h2>
        <span className={`numeric text-xs ${active ? 'text-sodium-500' : 'text-ink-500'}`}>
          {active ? (percent === null ? 'working' : `${percent}%`) : 'stopped'}
        </span>
      </div>

      {progress !== null && (
        <div className="mt-3 h-px w-full bg-ink-700">
          <div
            className="h-px origin-left bg-sodium-500 transition-transform duration-300"
            style={{ transform: `scaleX(${progress})` }}
          />
        </div>
      )}

      {remoteDownload && (
        <div className="mt-4 grid gap-3 border-y border-ink-800 py-3 text-xs sm:grid-cols-3">
          <TransferMetric
            label="Expected size"
            value={
              downloadMetrics.totalBytes === null
                ? 'calculating…'
                : `${downloadMetrics.totalIsEstimate ? '≈ ' : ''}${formatBytes(downloadMetrics.totalBytes)}`
            }
          />
          <TransferMetric
            label="Download speed"
            value={
              downloadMetrics.speedBytesS === null
                ? 'waiting…'
                : `${formatBytes(downloadMetrics.speedBytesS)}/s`
            }
          />
          <TransferMetric
            label="Downloaded"
            value={
              progress === null
                ? 'waiting…'
                : `${Math.round(progress * 100)}%${
                    downloadMetrics.downloadedBytes !== null
                      ? ` · ${formatBytes(downloadMetrics.downloadedBytes)}`
                      : ''
                  }`
            }
          />
        </div>
      )}

      <div className="mt-4 max-h-44 overflow-y-auto font-mono text-xs leading-relaxed">
        {entries.length === 0 ? (
          <p className="text-ink-600">Waiting for transfer activity…</p>
        ) : (
          entries.map((entry) => (
            <p key={entry.id} className="grid grid-cols-[5.5rem_1fr] gap-3 text-ink-400">
              <span className="numeric text-ink-600">{entry.time}</span>
              <span>{entry.message}</span>
            </p>
          ))
        )}
      </div>
    </section>
  )
}

function TransferMetric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="eyebrow block text-[10px]">{label}</span>
      <span className="numeric mt-1 block text-ink-300">{value}</span>
    </div>
  )
}

function AdvancedOptions({
  open,
  onToggle,
  overrides,
  onChange,
  providers,
}: {
  open: boolean
  onToggle: () => void
  overrides: JobSettingsOverrides
  onChange: (next: JobSettingsOverrides) => void
  providers: ProviderStatus[]
}) {
  const set = <K extends keyof JobSettingsOverrides>(key: K, value: JobSettingsOverrides[K]) =>
    onChange({ ...overrides, [key]: value })

  return (
    <div className="mt-8">
      <button type="button" onClick={onToggle} className="btn btn-quiet -ml-1">
        <span
          className="inline-block transition-transform duration-300"
          style={{ transform: open ? 'rotate(90deg)' : 'none' }}
          aria-hidden
        >
          ›
        </span>
        {open ? 'Hide options' : 'Options'}
      </button>

      {/* grid-template-rows rather than height, so the reveal animates without
          touching layout properties. */}
      <div
        className="grid transition-[grid-template-rows] duration-400 ease-[cubic-bezier(0.16,1,0.3,1)]"
        style={{ gridTemplateRows: open ? '1fr' : '0fr' }}
      >
        <div className="overflow-hidden">
          <div className="grid gap-x-8 gap-y-5 pt-5 sm:grid-cols-2">
            <Selector
              label="Provider"
              value={overrides.provider ?? ''}
              onChange={(v) => set('provider', v || undefined)}
              options={[
                { value: '', label: 'Use default' },
                ...providers.map((p) => ({
                  value: p.name,
                  label: p.available ? p.name : `${p.name} — unavailable`,
                  disabled: !p.available,
                })),
              ]}
            />
            <Selector
              label="Whisper model"
              value={overrides.whisper_model ?? ''}
              onChange={(v) => set('whisper_model', v || undefined)}
              options={[
                { value: '', label: 'Use default' },
                { value: 'tiny', label: 'tiny — fastest, roughest' },
                { value: 'base', label: 'base' },
                { value: 'small', label: 'small — balanced' },
                { value: 'medium', label: 'medium' },
                { value: 'large-v3', label: 'large-v3 — slowest, best' },
              ]}
            />
            <NumberField
              label="Max clips"
              value={overrides.max_clips}
              placeholder="10"
              min={1}
              max={50}
              onChange={(v) => set('max_clips', v)}
            />
            <div className="grid grid-cols-2 gap-4">
              <NumberField
                label="Min length (s)"
                value={overrides.min_duration_s}
                placeholder="20"
                min={5}
                max={300}
                onChange={(v) => set('min_duration_s', v)}
              />
              <NumberField
                label="Max length (s)"
                value={overrides.max_duration_s}
                placeholder="90"
                min={5}
                max={300}
                onChange={(v) => set('max_duration_s', v)}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function Selector({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string; disabled?: boolean }[]
}) {
  return (
    <label className="block">
      <span className="eyebrow">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="field mt-1 cursor-pointer text-sm"
      >
        {options.map((option) => (
          <option
            key={option.value}
            value={option.value}
            disabled={option.disabled}
            className="bg-ink-850"
          >
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}

function NumberField({
  label,
  value,
  placeholder,
  min,
  max,
  onChange,
}: {
  label: string
  value: number | undefined
  placeholder: string
  min: number
  max: number
  onChange: (value: number | undefined) => void
}) {
  return (
    <label className="block">
      <span className="eyebrow">{label}</span>
      <input
        type="number"
        className="field numeric mt-1 text-sm"
        placeholder={placeholder}
        value={value ?? ''}
        min={min}
        max={max}
        onChange={(e) => onChange(e.target.value ? Number(e.target.value) : undefined)}
      />
    </label>
  )
}

function RecentJobs({
  jobs,
  removingJobId,
  onRemove,
}: {
  jobs: Job[]
  removingJobId: string | null
  onRemove: (job: Job) => void
}) {
  if (jobs.length === 0) return null

  return (
    <section className="rise mt-24" style={{ animationDelay: '240ms' }}>
      <div className="flex items-baseline justify-between border-b border-ink-800 pb-3">
        <h2 className="eyebrow">Recent</h2>
        <span className="numeric text-xs text-ink-600">{jobs.length}</span>
      </div>

      <ul>
        {jobs.map((job) => {
          const removable = job.status !== 'queued' && job.status !== 'running'
          return (
            <li
              key={job.id}
              className="group flex items-stretch border-b border-ink-850 transition-colors duration-200 hover:bg-ink-850/40"
            >
              <a
                href={job.status === 'done' ? `/jobs/${job.id}/clips` : `/jobs/${job.id}`}
                className="grid min-w-0 flex-1 grid-cols-[1fr_auto] items-baseline gap-4 py-4 sm:grid-cols-[1fr_7rem_6rem_5rem]"
              >
                <span className="truncate text-[0.9375rem] text-ink-200 group-hover:text-ink-100">
                  {job.source?.title || 'Untitled'}
                  {job.highlight_pass > 1 && (
                    <span className="ml-2 text-xs text-ink-600">pass {job.highlight_pass}</span>
                  )}
                </span>
                <span className="numeric hidden text-xs text-ink-500 sm:block">
                  {job.source ? formatDuration(job.source.duration_s) : '—'}
                </span>
                <span className="hidden text-xs text-ink-500 sm:block">{job.provider}</span>
                <StatusTag job={job} />
              </a>

              {removable && (
                <button
                  type="button"
                  onClick={() => onRemove(job)}
                  disabled={removingJobId === job.id}
                  aria-label={`Remove ${job.source?.title || 'project'}`}
                  title="Remove project"
                  className="btn btn-quiet ml-3 shrink-0 self-center text-ink-600 opacity-100 transition-opacity hover:text-signal-bad sm:opacity-0 sm:group-hover:opacity-100 sm:focus:opacity-100"
                >
                  {removingJobId === job.id ? 'removing…' : 'remove'}
                </button>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}

function StatusTag({ job }: { job: Job }) {
  const tone: Record<string, string> = {
    done: 'text-signal-good',
    failed: 'text-signal-bad',
    running: 'text-sodium-500',
    queued: 'text-ink-400',
    cancelled: 'text-ink-500',
  }
  const label =
    job.status === 'running' ? `${Math.round(job.progress * 100)}%` : job.status

  return (
    <span className={`numeric justify-self-end text-xs ${tone[job.status] ?? 'text-ink-400'}`}>
      {label}
    </span>
  )
}
