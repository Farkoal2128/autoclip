import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import {
  api,
  formatBytes,
  formatDuration,
  type CaptionStyle,
  type Clip,
  type CropPath,
  type Job,
  type LayoutPreset,
  type Word,
} from '../api'
import { CaptionEditor } from '../components/CaptionEditor'
import { ClipPlayer } from '../components/ClipPlayer'
import { CutEditor } from '../components/CutEditor'
import { ErrorNote } from '../components/ErrorNote'
import { TrimBar } from '../components/TrimBar'

const RATIOS = ['9:16', '1:1', '16:9'] as const

export function Review() {
  const { jobId } = useParams()
  const navigate = useNavigate()
  const [job, setJob] = useState<Job | null>(null)
  const [clips, setClips] = useState<Clip[]>([])
  const [styles, setStyles] = useState<CaptionStyle[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [words, setWords] = useState<Word[]>([])
  const [cropPath, setCropPath] = useState<CropPath | null>(null)
  const [wordsDirty, setWordsDirty] = useState(false)
  const [savingWords, setSavingWords] = useState(false)
  const [savingCuts, setSavingCuts] = useState(false)
  const [savingLayout, setSavingLayout] = useState(false)
  const [savingTitle, setSavingTitle] = useState(false)
  const [playhead, setPlayhead] = useState(0)
  const [previewSeek, setPreviewSeek] = useState<{
    sourceTime: number
    requestId: number
  } | null>(null)
  const [exporting, setExporting] = useState<Set<string>>(new Set())
  const [exportingKept, setExportingKept] = useState(false)
  const [showRemoveDropped, setShowRemoveDropped] = useState(false)
  const [removingDropped, setRemovingDropped] = useState(false)
  const [findingMore, setFindingMore] = useState(false)
  const [layoutPresets, setLayoutPresets] = useState<LayoutPreset[]>([])
  const [presetBusy, setPresetBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    if (!jobId) return
    void Promise.all([
      api.getJob(jobId),
      api.listClips(jobId),
      api.captionStyles(),
      api.listLayoutPresets(),
    ])
      .then(([loadedJob, loadedClips, loadedStyles, loadedPresets]) => {
        setJob(loadedJob)
        setClips(loadedClips)
        setStyles(loadedStyles)
        setLayoutPresets(loadedPresets)
        setSelectedId((current) => current ?? loadedClips[0]?.id ?? null)
      })
      .catch((err) => setError(err as Error))

  }, [jobId])

  const selected = useMemo(
    () => clips.find((clip) => clip.id === selectedId) ?? null,
    [clips, selectedId],
  )

  useEffect(() => {
    if (!selected) return
    setWordsDirty(false)
    setPlayhead(selected.start_s)
    setPreviewSeek(null)
    api
      .getClipWords(selected.id)
      .then(setWords)
      .catch(() => setWords([]))
  }, [selected?.id])

  useLayoutEffect(() => {
    if (!selected) {
      setCropPath(null)
      return
    }

    let cancelled = false

    // Crop paths are generated for the clip's current output ratio. Clear the
    // previous geometry before paint so a 9:16 crop is never stretched inside
    // a newly selected 1:1 (or 16:9) preview frame.
    setCropPath(null)
    api
      .getCropPath(selected.id)
      .then((next) => {
        if (!cancelled) setCropPath(next)
      })
      .catch(() => {
        // 404 is expected for audio-only sources and jobs that never reframed;
        // the player falls back to a centre crop, matching what the renderer does.
        if (!cancelled) setCropPath(null)
      })

    return () => {
      cancelled = true
    }
  }, [selected?.id, selected?.ratio])

  const patchClip = useCallback((updated: Clip) => {
    setClips((current) => current.map((clip) => (clip.id === updated.id ? updated : clip)))
  }, [])

  const setStatus = async (clip: Clip, status: Clip['status']) => {
    try {
      patchClip(await api.patchClip(clip.id, { status }))
    } catch (err) {
      setError(err as Error)
    }
  }

  const renameClip = async (title: string) => {
    if (!selected) return
    const trimmed = title.trim()
    if (!trimmed || trimmed === selected.title) return

    setSavingTitle(true)
    try {
      patchClip(await api.patchClip(selected.id, { title: trimmed }))
    } catch (err) {
      setError(err as Error)
    } finally {
      setSavingTitle(false)
    }
  }

  const commitTrim = async (start: number, end: number) => {
    if (!selected) return
    try {
      patchClip(await api.patchClip(selected.id, { start_s: start, end_s: end }))
      setWords(await api.getClipWords(selected.id))
    } catch (err) {
      setError(err as Error)
    }
  }

  const seekPreviewToWord = (sourceTime: number) => {
    setPreviewSeek((current) => ({
      sourceTime,
      requestId: (current?.requestId ?? 0) + 1,
    }))
  }

  const saveWords = async () => {
    if (!selected) return
    setSavingWords(true)
    try {
      patchClip(await api.patchCaptions(selected.id, { words }))
      setWordsDirty(false)
    } catch (err) {
      setError(err as Error)
    } finally {
      setSavingWords(false)
    }
  }

  const saveCuts = async (nextCuts: Clip['cuts']) => {
    if (!selected) return
    setSavingCuts(true)
    try {
      patchClip(await api.patchCuts(selected.id, nextCuts))
    } catch (err) {
      setError(err as Error)
    } finally {
      setSavingCuts(false)
    }
  }

  const saveLayout = async (layout: Clip['layout']): Promise<boolean> => {
    if (!selected) return false
    setSavingLayout(true)
    try {
      patchClip(await api.patchLayout(selected.id, layout))
      return true
    } catch (err) {
      setError(err as Error)
      return false
    } finally {
      setSavingLayout(false)
    }
  }

  const saveLayoutPreset = async (
    name: string,
    ratio: LayoutPreset['ratio'],
    layout: LayoutPreset['layout'],
  ) => {
    const trimmed = name.trim()
    if (!trimmed) return
    setPresetBusy(true)
    setError(null)
    try {
      const created = await api.createLayoutPreset(trimmed, ratio, layout)
      setLayoutPresets((current) => [...current, created])
    } catch (err) {
      setError(err as Error)
    } finally {
      setPresetBusy(false)
    }
  }

  const applyLayoutPreset = async (preset: LayoutPreset) => {
    if (!selected) return
    setPresetBusy(true)
    setSavingLayout(true)
    setError(null)
    try {
      patchClip(await api.patchLayout(selected.id, preset.layout, preset.ratio))
    } catch (err) {
      setError(err as Error)
    } finally {
      setSavingLayout(false)
      setPresetBusy(false)
    }
  }

  const deleteLayoutPreset = async (preset: LayoutPreset) => {
    if (!window.confirm(`Delete layout preset "${preset.name}"?`)) return
    setPresetBusy(true)
    setError(null)
    try {
      await api.deleteLayoutPreset(preset.id)
      setLayoutPresets((current) => current.filter((item) => item.id !== preset.id))
    } catch (err) {
      setError(err as Error)
    } finally {
      setPresetBusy(false)
    }
  }

  const setCaptionsEnabled = async (clip: Clip, enabled: boolean) => {
    try {
      patchClip(await api.patchCaptions(clip.id, { burn_captions: enabled }))
    } catch (err) {
      setError(err as Error)
    }
  }

  const setStyle = async (clip: Clip, styleKey: string) => {
    try {
      patchClip(await api.patchCaptions(clip.id, { caption_style: styleKey }))
    } catch (err) {
      setError(err as Error)
    }
  }

  const setRatio = async (clip: Clip, ratio: string) => {
    if (ratio === clip.ratio) return
    try {
      patchClip(await api.patchCaptions(clip.id, { ratio }))
    } catch (err) {
      setError(err as Error)
    }
  }

  const exportClip = async (clip: Clip) => {
    setExporting((current) => new Set(current).add(clip.id))
    setError(null)
    try {
      await api.exportClip(clip.id, clip.ratio, clip.caption_style)
      patchClip(await api.getClip(clip.id))
    } catch (err) {
      setError(err as Error)
    } finally {
      setExporting((current) => {
        const next = new Set(current)
        next.delete(clip.id)
        return next
      })
    }
  }

  const exportKept = async () => {
    if (!jobId || exportingKept) return
    setExportingKept(true)
    setError(null)
    try {
      const archive = await api.exportKeptArchive(jobId)
      setClips(await api.listClips(jobId))

      const link = document.createElement('a')
      link.href = archive.download_url
      link.download = archive.filename
      document.body.appendChild(link)
      link.click()
      link.remove()
    } catch (err) {
      setError(err as Error)
    } finally {
      setExportingKept(false)
    }
  }

  const removeAllDropped = async () => {
    if (!jobId || removingDropped) return
    setRemovingDropped(true)
    setError(null)
    try {
      const result = await api.deleteDiscardedClips(jobId)
      const deleted = new Set(result.deleted_ids)
      const remaining = clips.filter((clip) => !deleted.has(clip.id))
      setClips(remaining)
      setShowRemoveDropped(false)
      if (selectedId && deleted.has(selectedId)) {
        setSelectedId(remaining[0]?.id ?? null)
      }
    } catch (err) {
      setError(err as Error)
    } finally {
      setRemovingDropped(false)
    }
  }

  const findMoreClips = async () => {
    if (!jobId) return
    if (
      !window.confirm(
        [
          'Create a new highlight project from this source?',
          'AutoClip will reuse the existing audio and transcript, skip transcription,',
          'use your current provider/clip settings, and ask for different moments',
          'than the clips already found.',
        ].join(' '),
      )
    ) {
      return
    }

    setFindingMore(true)
    setError(null)
    try {
      const next = await api.findMoreClips(jobId)
      navigate(`/jobs/${next.id}`)
    } catch (err) {
      setError(err as Error)
      setFindingMore(false)
    }
  }


  const keptCount = clips.filter((clip) => clip.status === 'kept').length
  const droppedClips = clips.filter((clip) => clip.status === 'discarded')
  const droppedCount = droppedClips.length
  const activeStyle = styles.find((style) => style.key === selected?.caption_style)

  if (error && clips.length === 0) {
    return (
      <div className="max-w-3xl pt-24">
        <ErrorNote error={error} />
      </div>
    )
  }

  if (!job) return <p className="pt-24 text-sm text-ink-500">Loading…</p>

  return (
    <div className="pt-10">
      <div className="flex flex-wrap items-baseline justify-between gap-4 border-b border-ink-800 pb-5">
        <div className="min-w-0">
          <Link to="/" className="eyebrow transition-colors hover:text-sodium-500">
            ← All jobs
          </Link>
          <h1 className="mt-2 max-w-2xl truncate font-display text-[clamp(1.5rem,3vw,2.25rem)] leading-tight text-ink-100">
            {job.source?.title || 'Untitled'}
          </h1>
        </div>

        <div className="flex flex-wrap items-baseline justify-end gap-3">
          <span className="numeric mr-3 text-xs text-ink-500">
            {clips.length} clips · {keptCount} kept
            {job.highlight_pass > 1 ? ` · pass ${job.highlight_pass}` : ''}
          </span>
          <button
            type="button"
            onClick={() => void findMoreClips()}
            disabled={findingMore}
            className="btn btn-ghost"
            title="Reuse this project's audio and transcript, then search for different highlights"
          >
            {findingMore ? 'Creating…' : 'Find more clips'}
          </button>
          <button
            type="button"
            onClick={() => setShowRemoveDropped(true)}
            disabled={droppedCount === 0 || removingDropped}
            className="btn btn-ghost text-signal-bad"
          >
            Remove all dropped{droppedCount > 0 ? ` (${droppedCount})` : ''}
          </button>
          <button
            onClick={() => void exportKept()}
            disabled={keptCount === 0 || exportingKept}
            className="btn btn-primary"
          >
            {exportingKept ? 'Exporting archive…' : 'Export kept'}
          </button>
        </div>
      </div>

      {error && (
        <div className="mt-6 max-w-3xl">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {showRemoveDropped && (
        <div
          className="fixed inset-0 z-[90] grid place-items-center bg-black/75 p-4 backdrop-blur-[2px]"
          role="dialog"
          aria-modal="true"
          aria-labelledby="remove-dropped-title"
        >
          <div className="w-full max-w-lg border border-ink-700 bg-ink-900 p-5 shadow-2xl">
            <p className="eyebrow">Permanent deletion</p>
            <h2
              id="remove-dropped-title"
              className="mt-2 font-display text-2xl text-ink-100"
            >
              Remove all dropped shorts?
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-ink-400">
              These {droppedCount} dropped short{droppedCount === 1 ? '' : 's'} and any rendered
              export files attached to them will be permanently deleted.
            </p>

            <ul className="mt-4 max-h-64 overflow-y-auto border-y border-ink-800 py-2">
              {droppedClips.map((clip) => (
                <li
                  key={clip.id}
                  className="flex items-baseline gap-3 border-b border-ink-850 px-1 py-2 last:border-b-0"
                >
                  <span className="numeric w-7 shrink-0 text-xs text-ink-600">
                    #{clip.rank}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-sm text-ink-200">
                    {clip.title || 'Untitled clip'}
                  </span>
                  <span className="numeric shrink-0 text-xs text-ink-600">
                    {formatDuration(clip.duration_s)}
                  </span>
                </li>
              ))}
            </ul>

            <div className="mt-5 grid gap-2 sm:grid-cols-2">
              <button
                type="button"
                onClick={() => setShowRemoveDropped(false)}
                disabled={removingDropped}
                className="btn btn-ghost"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void removeAllDropped()}
                disabled={removingDropped}
                className="btn btn-ghost border-signal-bad/60 text-signal-bad"
              >
                {removingDropped ? 'Deleting…' : `Delete ${droppedCount} dropped`}
              </button>
            </div>
          </div>
        </div>
      )}

      {clips.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="mt-8 grid gap-x-10 gap-y-10 lg:grid-cols-[minmax(0,20rem)_minmax(0,26rem)_minmax(0,1fr)]">
          {/* Ranked list */}
          <section className="lg:max-h-[76vh] lg:overflow-y-auto lg:pr-1">
            <p className="eyebrow border-b border-ink-800 pb-2">Ranked</p>
            <ul>
              {clips.map((clip, index) => (
                <li key={clip.id}>
                  <ClipRow
                    clip={clip}
                    index={index}
                    selected={clip.id === selectedId}
                    onSelect={() => setSelectedId(clip.id)}
                    onKeep={() =>
                      setStatus(clip, clip.status === 'kept' ? 'candidate' : 'kept')
                    }
                    onDiscard={() =>
                      setStatus(clip, clip.status === 'discarded' ? 'candidate' : 'discarded')
                    }
                  />
                </li>
              ))}
            </ul>
          </section>

          {/* Player + trim */}
          <section className="space-y-8">
            {selected && jobId && (
              <>
                <ClipPlayer
                  src={api.mediaUrl(jobId)}
                  startS={selected.start_s}
                  endS={selected.end_s}
                  words={words}
                  style={activeStyle}
                  ratio={selected.ratio}
                  cropPath={cropPath}
                  layout={selected.layout}
                  sourceWidth={job.source?.width ?? null}
                  sourceHeight={job.source?.height ?? null}
                  layoutSaving={savingLayout}
                  onLayoutSave={saveLayout}
                  layoutPresets={layoutPresets}
                  layoutPresetBusy={presetBusy}
                  onLayoutPresetSave={(name, ratio, layout) =>
                    void saveLayoutPreset(name, ratio, layout)
                  }
                  onLayoutPresetApply={(preset) => void applyLayoutPreset(preset)}
                  onLayoutPresetDelete={(preset) => void deleteLayoutPreset(preset)}
                  captionsEnabled={selected.burn_captions}
                  cuts={selected.cuts}
                  seekRequest={previewSeek}
                  onTimeChange={setPlayhead}
                />
                <TrimBar
                  words={words}
                  startS={selected.start_s}
                  endS={selected.end_s}
                  originalStart={selected.start_s}
                  originalEnd={selected.end_s}
                  cuts={selected.cuts}
                  onCommit={commitTrim}
                />
                <CutEditor
                  cuts={selected.cuts}
                  currentTime={playhead}
                  startS={selected.start_s}
                  endS={selected.end_s}
                  words={words}
                  busy={savingCuts}
                  onChange={(next) => void saveCuts(next)}
                />
              </>
            )}
          </section>

          {/* Editor + style */}
          <section className="space-y-10">
            {selected && (
              <>
                <ClipTitleEditor
                  clip={selected}
                  saving={savingTitle}
                  onSave={(title) => void renameClip(title)}
                />

                <div>
                  <p className="eyebrow">Why this clip</p>
                  <p className="mt-2 max-w-prose text-[0.9375rem] leading-relaxed text-ink-300">
                    {selected.reason || 'No rationale returned for this clip.'}
                  </p>
                  {selected.hook && (
                    <p className="mt-3 border-l-2 border-ink-700 pl-3 font-display text-lg italic text-ink-200">
                      “{selected.hook}”
                    </p>
                  )}
                </div>

                <CaptionEditor
                  words={words}
                  clipStartS={selected.start_s}
                  clipEndS={selected.end_s}
                  onChange={(next) => {
                    setWords(next)
                    setWordsDirty(true)
                  }}
                  onSeek={seekPreviewToWord}
                  onSave={saveWords}
                  saving={savingWords}
                  dirty={wordsDirty}
                />

                <div>
                  <p className="eyebrow border-b border-ink-800 pb-2">Captions</p>
                  <label className="mt-3 flex items-start gap-3 text-sm text-ink-200">
                    <input
                      type="checkbox"
                      checked={selected.burn_captions}
                      onChange={(e) => void setCaptionsEnabled(selected, e.target.checked)}
                      className="mt-0.5 size-4 accent-sodium-500"
                    />
                    <span>
                      Burn captions into video
                      <span className="mt-1 block text-xs leading-snug text-ink-500">
                        Turn this off for a clean video export. Your transcript edits are kept.
                      </span>
                    </span>
                  </label>

                  <div
                    className={[
                      'mt-5 space-y-1 transition-opacity duration-200',
                      selected.burn_captions ? '' : 'opacity-40',
                    ].join(' ')}
                  >
                    {styles.map((style) => (
                      <button
                        key={style.key}
                        disabled={!selected.burn_captions}
                        onClick={() => setStyle(selected, style.key)}
                        className={[
                          'block w-full border-l-2 py-2 pl-3 text-left transition-colors duration-200',
                          style.key === selected.caption_style
                            ? 'border-sodium-500 bg-ink-850/60'
                            : 'border-transparent hover:border-ink-700 hover:bg-ink-850/30',
                        ].join(' ')}
                      >
                        <span className="text-sm text-ink-100">{style.label}</span>
                        <span className="mt-0.5 block text-xs leading-snug text-ink-500">
                          {style.description}
                        </span>
                      </button>
                    ))}
                  </div>
                </div>

                <div>
                  <p className="eyebrow border-b border-ink-800 pb-2">Aspect ratio</p>
                  <div className="mt-3 flex gap-2">
                    {RATIOS.map((ratio) => (
                      <button
                        key={ratio}
                        onClick={() => setRatio(selected, ratio)}
                        className={[
                          'numeric btn',
                          ratio === selected.ratio ? 'btn-primary' : 'btn-ghost',
                        ].join(' ')}
                      >
                        {ratio}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="border-t border-ink-800 pt-6">
                  <button
                    onClick={() => exportClip(selected)}
                    disabled={exporting.has(selected.id)}
                    className="btn btn-primary w-full"
                  >
                    {exporting.has(selected.id) ? 'Rendering…' : 'Export this clip'}
                  </button>

                  {selected.exports.length > 0 && (
                    <ul className="mt-4 space-y-2">
                      {selected.exports.map((record) => (
                        <li
                          key={record.id}
                          className="flex items-baseline justify-between gap-3 text-xs"
                        >
                          <a
                            href={record.download_url}
                            download
                            className="truncate text-sodium-500 underline underline-offset-4"
                          >
                            {record.ratio} · {record.style}
                          </a>
                          <span className="numeric shrink-0 text-ink-600">
                            {formatBytes(record.size_bytes)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>

              </>
            )}
          </section>
        </div>
      )}
    </div>
  )
}

function ClipTitleEditor({
  clip,
  saving,
  onSave,
}: {
  clip: Clip
  saving: boolean
  onSave: (title: string) => void
}) {
  const [draft, setDraft] = useState(clip.title)

  useEffect(() => setDraft(clip.title), [clip.id, clip.title])

  const commit = () => {
    const next = draft.trim()
    if (!next) {
      setDraft(clip.title)
      return
    }
    if (next !== clip.title) onSave(next)
  }

  return (
    <label className="block">
      <span className="flex items-baseline justify-between gap-3 border-b border-ink-800 pb-2">
        <span className="eyebrow">Clip title</span>
        <span className="text-xs text-ink-600">{saving ? 'saving…' : 'used for export filename'}</span>
      </span>
      <input
        value={draft}
        disabled={saving}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') event.currentTarget.blur()
          if (event.key === 'Escape') {
            setDraft(clip.title)
            event.currentTarget.blur()
          }
        }}
        className="field mt-3 font-display text-lg"
        aria-label="Clip title"
        spellCheck={false}
      />
      <span className="mt-1.5 block text-xs leading-snug text-ink-500">
        Rename the clip here. The ranked list updates immediately, and future exports use the
        new title in the filename.
      </span>
    </label>
  )
}

function ClipRow({
  clip,
  index,
  selected,
  onSelect,
  onKeep,
  onDiscard,
}: {
  clip: Clip
  index: number
  selected: boolean
  onSelect: () => void
  onKeep: () => void
  onDiscard: () => void
}) {
  return (
    <div
      className={[
        'rise group grid cursor-pointer grid-cols-[2.25rem_1fr] gap-x-3 border-b border-ink-850 py-3 pl-2 transition-colors duration-200',
        selected ? 'bg-ink-850/70' : 'hover:bg-ink-850/35',
        clip.status === 'discarded' ? 'opacity-40' : '',
      ].join(' ')}
      style={{ animationDelay: `${Math.min(index, 10) * 35}ms` }}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onSelect()
      }}
    >
      {/* The score is the thing you scan by, so it gets the display face. */}
      <span
        className={[
          'numeric self-start font-display text-2xl leading-none',
          clip.score >= 85 ? 'text-sodium-500' : selected ? 'text-ink-200' : 'text-ink-500',
        ].join(' ')}
      >
        {clip.score}
      </span>

      <div className="min-w-0">
        <p className="truncate text-[0.9375rem] leading-snug text-ink-100">
          {clip.title || 'Untitled clip'}
        </p>
        <div className="mt-1 flex items-baseline gap-3">
          <span className="numeric text-xs text-ink-500">{formatDuration(clip.duration_s)}</span>
          {clip.user_trimmed && <span className="text-xs text-ink-600">trimmed</span>}
          {clip.exports.length > 0 && (
            <span className="text-xs text-signal-good">exported</span>
          )}

          {/* Secondary actions appear on hover or when the row is current —
              progressive disclosure keeps the scan column clean. */}
          <span
            className={[
              'ml-auto flex gap-3 pr-2 transition-opacity duration-200',
              selected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100',
            ].join(' ')}
          >
            <button
              onClick={(e) => {
                e.stopPropagation()
                onKeep()
              }}
              className={`text-xs ${clip.status === 'kept' ? 'text-signal-good' : 'text-ink-400 hover:text-ink-100'}`}
            >
              {clip.status === 'kept' ? 'kept' : 'keep'}
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation()
                onDiscard()
              }}
              className="text-xs text-ink-400 hover:text-signal-bad"
            >
              {clip.status === 'discarded' ? 'undo' : 'drop'}
            </button>
          </span>
        </div>
      </div>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="max-w-xl pt-20">
      <h2 className="font-display text-3xl text-ink-200">No clips came back.</h2>
      <p className="mt-4 text-sm leading-relaxed text-ink-400">
        The model found nothing self-contained enough to stand alone — which is a real
        answer for some source material, not necessarily a failure.
      </p>
      <p className="mt-3 text-sm leading-relaxed text-ink-400">
        If you expected clips, try a larger provider model, or widen the length range so
        shorter moments qualify.
      </p>
    </div>
  )
}
