import { useEffect, useRef, useState } from 'react'

import {
  api,
  formatBytes,
  type DesktopShortcutStatus,
  type ProviderStatus,
  type Settings as SettingsData,
  type StorageMoveActivityEvent,
  type StorageStatus,
  type SystemStatus,
} from '../api'
import { ErrorNote } from '../components/ErrorNote'

const SECRET_LABELS: Record<string, string> = {
  anthropic: 'Anthropic API key',
  openai: 'OpenAI-compatible API key',
  gemini: 'Google Gemini API key',
  huggingface_token: 'HuggingFace token',
}

export function Settings() {
  const [settings, setSettings] = useState<SettingsData | null>(null)
  const [providers, setProviders] = useState<ProviderStatus[]>([])
  const [system, setSystem] = useState<SystemStatus | null>(null)
  const [shortcut, setShortcut] = useState<DesktopShortcutStatus | null>(null)
  const [shortcutBusy, setShortcutBusy] = useState(false)
  const [storage, setStorage] = useState<StorageStatus | null>(null)
  const [storageDraft, setStorageDraft] = useState('')
  const [storageBusy, setStorageBusy] = useState(false)
  const [storageMoveLog, setStorageMoveLog] = useState<StorageMoveLogEntry[]>([])
  const [storageMoveProgress, setStorageMoveProgress] = useState<number | null>(null)
  const [storageMoveFolder, setStorageMoveFolder] = useState<string | null>(null)
  const [storageMoveComplete, setStorageMoveComplete] = useState(false)
  const storageMoveLogId = useRef(0)
  const [error, setError] = useState<Error | null>(null)
  const [saved, setSaved] = useState(false)

  const reload = () => {
    void api.getSettings().then(setSettings).catch((e) => setError(e as Error))
    void api.providerStatus().then(setProviders).catch(() => undefined)
    void api.system().then(setSystem).catch(() => undefined)
    void api.getDesktopShortcut().then(setShortcut).catch(() => undefined)
    void api
      .getStorage()
      .then((current) => {
        setStorage(current)
        setStorageDraft(current.path)
      })
      .catch(() => undefined)
  }

  useEffect(reload, [])

  const patch = async (update: Partial<SettingsData>) => {
    setError(null)
    try {
      setSettings(await api.putSettings(update))
      setSaved(true)
      setTimeout(() => setSaved(false), 1600)
    } catch (err) {
      setError(err as Error)
    }
  }

  const browseStorage = async () => {
    setError(null)
    try {
      const choice = await api.browseStorage()
      if (choice.path) setStorageDraft(choice.path)
    } catch (err) {
      setError(err as Error)
    }
  }

  const appendStorageMoveLog = (message: string) => {
    setStorageMoveLog((current) => {
      if (current[current.length - 1]?.message === message) return current
      storageMoveLogId.current += 1
      return [
        ...current,
        {
          id: storageMoveLogId.current,
          time: new Date().toLocaleTimeString(),
          message,
        },
      ].slice(-60)
    })
  }

  const onStorageMoveEvent = (event: StorageMoveActivityEvent) => {
    if (event.type === 'progress' && event.progress !== undefined) {
      setStorageMoveProgress(Math.max(0, Math.min(1, event.progress)))
      setStorageMoveFolder(event.folder ?? null)
      return
    }
    if (event.message) appendStorageMoveLog(event.message)
  }

  const createShortcut = async () => {
    setShortcutBusy(true)
    setError(null)
    try {
      setShortcut(await api.createDesktopShortcut())
    } catch (err) {
      setError(err as Error)
    } finally {
      setShortcutBusy(false)
    }
  }

  const removeShortcut = async () => {
    setShortcutBusy(true)
    setError(null)
    try {
      await api.deleteDesktopShortcut()
      setShortcut(await api.getDesktopShortcut())
    } catch (err) {
      setError(err as Error)
    } finally {
      setShortcutBusy(false)
    }
  }

  const moveStorage = async () => {
    if (!storage || !storageDraft.trim() || storageDraft.trim() === storage.path) return
    const destination = storageDraft.trim()
    if (
      !window.confirm(
        `Move AutoClip media, work files, and exports to "${destination}"? Existing files will be moved so current projects keep working.`,
      )
    ) {
      return
    }

    setStorageBusy(true)
    setStorageMoveComplete(false)
    setStorageMoveProgress(0)
    setStorageMoveFolder(null)
    storageMoveLogId.current += 1
    setStorageMoveLog([
      {
        id: storageMoveLogId.current,
        time: new Date().toLocaleTimeString(),
        message: `Moving storage to ${destination}`,
      },
    ])
    setError(null)
    try {
      const next = await api.moveStorage(destination, onStorageMoveEvent)
      setStorage(next)
      setStorageDraft(next.path)
      setStorageMoveProgress(1)
      setStorageMoveFolder(null)
      setStorageMoveComplete(true)
      appendStorageMoveLog('Storage move complete')
      setSaved(true)
      setTimeout(() => setSaved(false), 1600)
    } catch (err) {
      setStorageMoveComplete(false)
      setStorageMoveFolder(null)
      appendStorageMoveLog('Storage move stopped with an error')
      setError(err as Error)
    } finally {
      setStorageBusy(false)
    }
  }

  if (!settings) return <p className="pt-24 text-sm text-ink-500">Loading…</p>

  return (
    <div className="max-w-4xl pt-14">
      <div className="rise flex items-baseline justify-between border-b border-ink-800 pb-5">
        <h1 className="font-display text-[clamp(2rem,4vw,3rem)] leading-none text-ink-100">
          Settings
        </h1>
        <span
          className="text-xs text-signal-good transition-opacity duration-300"
          style={{ opacity: saved ? 1 : 0 }}
        >
          saved
        </span>
      </div>

      {error && (
        <div className="mt-6">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {settings.insecure_secret_storage && (
        <p className="mt-6 border-l-2 border-sodium-600 pl-4 text-sm leading-relaxed text-ink-300">
          No OS keyring is available on this machine, so API keys are stored in plain text
          in <code className="text-ink-200">config.json</code>. On headless Linux, installing
          a Secret Service provider or <code className="text-ink-200">keyrings.alt</code>{' '}
          fixes this.
        </p>
      )}

      <Section title="AI provider" note="Which model picks the clips.">
        <div className="space-y-1">
          {providers.map((provider) => (
            <button
              key={provider.name}
              onClick={() => patch({ active_provider: provider.name })}
              className={[
                'block w-full border-l-2 py-3 pl-3 text-left transition-colors duration-200',
                provider.name === settings.active_provider
                  ? 'border-sodium-500 bg-ink-850/60'
                  : 'border-transparent hover:border-ink-700 hover:bg-ink-850/30',
              ].join(' ')}
            >
              <div className="flex items-baseline justify-between gap-4">
                <span className="text-sm text-ink-100">{provider.name}</span>
                <span
                  className={`text-xs ${provider.available ? 'text-signal-good' : 'text-ink-500'}`}
                >
                  {provider.available ? 'reachable' : provider.detail || 'unavailable'}
                </span>
              </div>
              {provider.models.length > 0 && (
                <span className="numeric mt-1 block truncate text-xs text-ink-600">
                  {provider.models.slice(0, 6).join(' · ')}
                </span>
              )}
            </button>
          ))}
        </div>

        <div className="mt-6 grid gap-5 sm:grid-cols-2">
          <Field
            label="Model"
            value={settings.providers[settings.active_provider]?.model ?? ''}
            placeholder="model name"
            onCommit={(value) =>
              patch({
                providers: {
                  [settings.active_provider]: {
                    ...settings.providers[settings.active_provider],
                    model: value,
                  },
                },
              })
            }
          />
          <Field
            label="Base URL"
            hint="Point at OpenRouter, Groq, DeepSeek, or a local server."
            value={settings.providers[settings.active_provider]?.base_url ?? ''}
            placeholder="https://…"
            onCommit={(value) =>
              patch({
                providers: {
                  [settings.active_provider]: {
                    ...settings.providers[settings.active_provider],
                    base_url: value || null,
                  },
                },
              })
            }
          />
        </div>
      </Section>

      <Section title="Keys" note="Stored in your OS keyring. Never sent anywhere but the provider.">
        <div className="space-y-4">
          {Object.entries(SECRET_LABELS).map(([key, label]) => (
            <SecretField
              key={key}
              secretKey={key}
              label={label}
              present={settings.keys_present[key] ?? false}
              onChanged={reload}
              onError={setError}
            />
          ))}
        </div>
      </Section>

      <Section title="Transcription">
        <div className="grid gap-5 sm:grid-cols-2">
          <Select
            label="Whisper model"
            value={settings.whisper.model}
            onChange={(value) => patch({ whisper: { ...settings.whisper, model: value } })}
            options={['tiny', 'base', 'small', 'medium', 'large-v3']}
          />
          <Field
            label="Language"
            hint="Leave empty to detect automatically."
            value={settings.whisper.language}
            placeholder="auto"
            onCommit={(value) => patch({ whisper: { ...settings.whisper, language: value } })}
          />
        </div>

        <label className="mt-5 flex items-start gap-3 text-sm text-ink-200">
          <input
            type="checkbox"
            checked={settings.whisper.diarization}
            disabled={!system?.diarization_available}
            onChange={(e) =>
              patch({ whisper: { ...settings.whisper, diarization: e.target.checked } })
            }
            className="mt-0.5 size-4 accent-sodium-500"
          />
          <span>
            Identify speakers
            {!system?.diarization_available && (
              <span className="mt-1 block text-xs text-ink-500">
                Needs the diarization extra:{' '}
                <code className="text-ink-300">uv pip install &apos;autoclip[diarization]&apos;</code>
              </span>
            )}
          </span>
        </label>
      </Section>

      <Section title="Clips">
        <div className="grid gap-5 sm:grid-cols-3">
          <NumberField
            label="Min length (s)"
            value={settings.clips.min_duration_s}
            onCommit={(value) => patch({ clips: { ...settings.clips, min_duration_s: value } })}
          />
          <NumberField
            label="Max length (s)"
            value={settings.clips.max_duration_s}
            onCommit={(value) => patch({ clips: { ...settings.clips, max_duration_s: value } })}
          />
          <NumberField
            label="Max clips"
            value={settings.clips.max_clips}
            onCommit={(value) => patch({ clips: { ...settings.clips, max_clips: value } })}
          />
        </div>
      </Section>

      {storage && (
        <Section
          title="Local storage"
          note="Choose where AutoClip keeps large media, work files, and exports."
        >
          <div className="grid gap-4">
            <div className="flex flex-wrap items-end gap-3">
              <label className="min-w-64 flex-1">
                <span className="eyebrow">Storage folder</span>
                <input
                  className="field mt-1 text-sm"
                  value={storageDraft}
                  disabled={storageBusy || storage.managed_by_env}
                  onChange={(event) => setStorageDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') void moveStorage()
                  }}
                  spellCheck={false}
                />
              </label>
              <button
                type="button"
                onClick={() => void browseStorage()}
                disabled={storageBusy || storage.managed_by_env}
                className="btn btn-ghost"
              >
                Browse…
              </button>
              <button
                type="button"
                onClick={() => void moveStorage()}
                disabled={
                  storageBusy ||
                  storage.managed_by_env ||
                  !storageDraft.trim() ||
                  storageDraft.trim() === storage.path
                }
                className="btn btn-primary"
              >
                {storageBusy ? 'Moving files…' : 'Move storage'}
              </button>
              {storage.custom && !storage.managed_by_env && (
                <button
                  type="button"
                  onClick={() => setStorageDraft(storage.control_path)}
                  disabled={storageBusy}
                  className="btn btn-quiet"
                >
                  Use default
                </button>
              )}
            </div>

            <div className="grid gap-x-8 gap-y-2 text-xs text-ink-500 sm:grid-cols-2">
              <p>
                Free space:{' '}
                <span className="numeric text-ink-300">
                  {formatBytes(storage.free_bytes)} of {formatBytes(storage.total_bytes)}
                </span>
              </p>
              <p className="truncate" title={storage.control_path}>
                Control files stay in{' '}
                <code className="text-ink-300">{storage.control_path}</code>
              </p>
            </div>

            <p className="text-xs leading-relaxed text-ink-500">
              Changing this moves the existing <code className="text-ink-300">media</code>,{' '}
              <code className="text-ink-300">work</code>, and{' '}
              <code className="text-ink-300">exports</code> folders so current projects keep
              working. The small database and config file stay in your normal AutoClip control
              folder.
            </p>

            {(storageBusy || storageMoveLog.length > 0 || storageMoveComplete) && (
              <div className="border border-ink-800 bg-ink-850/35 p-4">
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                  <p className="eyebrow">Storage move activity</p>
                  {storageBusy ? (
                    <span className="numeric text-xs text-sodium-500">
                      {Math.round((storageMoveProgress ?? 0) * 100)}%
                      {storageMoveFolder ? ` · ${storageMoveFolder}` : ''}
                    </span>
                  ) : storageMoveComplete ? (
                    <span className="text-xs text-signal-good">✓ complete</span>
                  ) : (
                    <span className="text-xs text-ink-600">stopped</span>
                  )}
                </div>

                <div className="mt-3 h-px w-full bg-ink-700">
                  <div
                    className={[
                      'h-px origin-left transition-transform duration-300',
                      storageMoveComplete ? 'bg-signal-good' : 'bg-sodium-500',
                    ].join(' ')}
                    style={{
                      transform: `scaleX(${storageMoveProgress ?? 0})`,
                    }}
                  />
                </div>

                <div className="mt-4 max-h-48 overflow-y-auto font-mono text-xs leading-relaxed">
                  {storageMoveLog.map((entry) => (
                    <p
                      key={entry.id}
                      className="grid grid-cols-[5.5rem_1fr] gap-3 border-b border-ink-850/60 py-1 text-ink-400"
                    >
                      <span className="numeric text-ink-600">{entry.time}</span>
                      <span>{entry.message}</span>
                    </p>
                  ))}
                </div>
              </div>
            )}

            {storage.managed_by_env && (
              <p className="border-l-2 border-sodium-600 pl-3 text-xs leading-relaxed text-ink-400">
                This folder is controlled by{' '}
                <code className="text-ink-200">AUTOCLIP_STORAGE_HOME</code>. Remove that
                environment variable before changing it here.
              </p>
            )}
          </div>
        </Section>
      )}

      <Section title="Ingest">
        <div className="grid gap-5 sm:grid-cols-2">
          <Select
            label="YouTube cookies from"
            hint="YouTube blocks most anonymous downloads. Sign in in that browser and close it before downloading."
            value={settings.ingest.cookies_from_browser}
            onChange={(value) =>
              patch({ ingest: { ...settings.ingest, cookies_from_browser: value } })
            }
            options={['', 'chrome', 'firefox', 'edge', 'brave', 'chromium', 'safari']}
            labels={{ '': 'None' }}
          />
        </div>
      </Section>

      <Section title="Export">
        <div className="grid gap-5 sm:grid-cols-2">
          <Select
            label="Default ratio"
            value={settings.export.ratio}
            onChange={(value) => patch({ export: { ...settings.export, ratio: value } })}
            options={['9:16', '1:1', '16:9']}
          />
          <NumberField
            label="Loudness target (LUFS)"
            value={settings.export.loudness_lufs}
            onCommit={(value) => patch({ export: { ...settings.export, loudness_lufs: value } })}
          />
        </div>

        <label className="mt-5 flex items-start gap-3 text-sm text-ink-200">
          <input
            type="checkbox"
            checked={settings.export.prefer_hardware_encoder}
            onChange={(e) =>
              patch({
                export: { ...settings.export, prefer_hardware_encoder: e.target.checked },
              })
            }
            className="mt-0.5 size-4 accent-sodium-500"
          />
          <span>
            Use GPU encoding when available
            {system && !system.nvenc_works && (
              <span className="mt-1 block text-xs text-ink-500">
                Not usable on this machine — exports will use the CPU encoder. Same quality,
                slower.
              </span>
            )}
          </span>
        </label>

        <label className="mt-4 flex items-center gap-3 text-sm text-ink-200">
          <input
            type="checkbox"
            checked={settings.export.write_srt}
            onChange={(e) => patch({ export: { ...settings.export, write_srt: e.target.checked } })}
            className="size-4 accent-sodium-500"
          />
          Also write an .srt sidecar
        </label>
      </Section>

      {system && (
        <Section title="This machine">
          <dl className="grid gap-x-8 gap-y-3 text-sm sm:grid-cols-2">
            <Row label="Platform" value={system.platform} />
            <Row label="Python" value={system.python_version} />
            <Row label="ffmpeg" value={system.ffmpeg_version ?? 'not found'} />
            <Row label="Acceleration" value={system.accel.toUpperCase()} />
            <Row label="Device" value={system.gpu_name ?? '—'} />
            <Row label="Whisper compute" value={system.compute_type} />
            <Row label="GPU encode" value={system.nvenc_works ? 'available' : 'unavailable'} />
            <Row label="Captions" value={system.has_libass ? 'libass present' : 'libass missing'} />
          </dl>

          {shortcut?.supported && (
            <div className="mt-6 border-t border-ink-800 pt-5">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div className="max-w-xl">
                  <p className="eyebrow">Desktop shortcut</p>
                  <p className="mt-1 text-xs leading-relaxed text-ink-500">
                    Launch AutoClip from your Windows desktop without opening PowerShell. If
                    AutoClip is already running, the shortcut simply opens it in your browser.
                  </p>
                  {shortcut.exists && shortcut.path && (
                    <p className="mt-2 truncate text-xs text-signal-good" title={shortcut.path}>
                      ✓ Installed at {shortcut.path}
                    </p>
                  )}
                </div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => void createShortcut()}
                    disabled={shortcutBusy}
                    className="btn btn-primary"
                  >
                    {shortcutBusy
                      ? 'Working…'
                      : shortcut.exists
                        ? 'Recreate shortcut'
                        : 'Create desktop shortcut'}
                  </button>
                  {shortcut.exists && (
                    <button
                      type="button"
                      onClick={() => void removeShortcut()}
                      disabled={shortcutBusy}
                      className="btn btn-quiet"
                    >
                      Remove
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}
        </Section>
      )}
    </div>
  )
}

type StorageMoveLogEntry = {
  id: number
  time: string
  message: string
}

function Section({
  title,
  note,
  children,
}: {
  title: string
  note?: string
  children: React.ReactNode
}) {
  return (
    <section className="rise mt-14">
      <div className="border-b border-ink-800 pb-2">
        <h2 className="eyebrow">{title}</h2>
        {note && <p className="mt-1 text-xs text-ink-500">{note}</p>}
      </div>
      <div className="mt-5">{children}</div>
    </section>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-ink-850 pb-2">
      <dt className="text-ink-500">{label}</dt>
      <dd className="numeric truncate text-right text-ink-200">{value}</dd>
    </div>
  )
}

/** Commits on blur rather than per keystroke, so a PUT isn't fired per letter. */
function Field({
  label,
  hint,
  value,
  placeholder,
  onCommit,
}: {
  label: string
  hint?: string
  value: string
  placeholder?: string
  onCommit: (value: string) => void
}) {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])

  return (
    <label className="block">
      <span className="eyebrow">{label}</span>
      <input
        className="field mt-1 text-sm"
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => draft !== value && onCommit(draft)}
        onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
        spellCheck={false}
      />
      {hint && <span className="mt-1.5 block text-xs leading-snug text-ink-500">{hint}</span>}
    </label>
  )
}

function NumberField({
  label,
  value,
  onCommit,
}: {
  label: string
  value: number
  onCommit: (value: number) => void
}) {
  const [draft, setDraft] = useState(String(value))
  useEffect(() => setDraft(String(value)), [value])

  return (
    <label className="block">
      <span className="eyebrow">{label}</span>
      <input
        type="number"
        className="field numeric mt-1 text-sm"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          const parsed = Number(draft)
          if (!Number.isNaN(parsed) && parsed !== value) onCommit(parsed)
        }}
        onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
      />
    </label>
  )
}

function Select({
  label,
  hint,
  value,
  onChange,
  options,
  labels = {},
}: {
  label: string
  hint?: string
  value: string
  onChange: (value: string) => void
  options: string[]
  labels?: Record<string, string>
}) {
  return (
    <label className="block">
      <span className="eyebrow">{label}</span>
      <select
        className="field mt-1 cursor-pointer text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((option) => (
          <option key={option} value={option} className="bg-ink-850">
            {labels[option] ?? option}
          </option>
        ))}
      </select>
      {hint && <span className="mt-1.5 block text-xs leading-snug text-ink-500">{hint}</span>}
    </label>
  )
}

function SecretField({
  secretKey,
  label,
  present,
  onChanged,
  onError,
}: {
  secretKey: string
  label: string
  present: boolean
  onChanged: () => void
  onError: (error: Error) => void
}) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)

  const save = async () => {
    if (!value.trim()) return
    setBusy(true)
    try {
      await api.putSecret(secretKey, value.trim())
      setValue('')
      onChanged()
    } catch (err) {
      onError(err as Error)
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    setBusy(true)
    try {
      await api.deleteSecret(secretKey)
      onChanged()
    } catch (err) {
      onError(err as Error)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-wrap items-end gap-3">
      <label className="min-w-56 flex-1">
        <span className="eyebrow">
          {label}
          {present && <span className="ml-2 text-signal-good">set</span>}
        </span>
        <input
          type="password"
          className="field mt-1 text-sm"
          value={value}
          placeholder={present ? '••••••••••••' : 'paste to add'}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && save()}
          autoComplete="off"
        />
      </label>
      <button onClick={save} disabled={!value.trim() || busy} className="btn btn-ghost">
        Save
      </button>
      {present && (
        <button onClick={remove} disabled={busy} className="btn btn-quiet">
          Remove
        </button>
      )}
    </div>
  )
}
