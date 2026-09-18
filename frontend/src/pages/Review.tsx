import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import {
  api,
  formatBytes,
  formatDuration,
  type CaptionStyle,
  type Clip,
  type CropPath,
  type Job,
  type PodcastResult,
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
  const [exporting, setExporting] = useState<Set<string>>(new Set())
  const [podcast, setPodcast] = useState<PodcastResult | null>(null)
  const [podcastBusy, setPodcastBusy] = useState(false)
  const [podcastProgress, setPodcastProgress] = useState<number | null>(null)
  const [podcastMessage, setPodcastMessage] = useState('')
  const [podcastRemoveSilences, setPodcastRemoveSilences] = useState(true)
  const [podcastRemoveBoring, setPodcastRemoveBoring] = useState(true)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    if (!jobId) return
    void Promise.all([api.getJob(jobId), api.listClips(jobId), api.captionStyles()])
      .then(([loadedJob, loadedClips, loadedStyles]) => {
        setJob(loadedJob)
        setClips(loadedClips)
        setStyles(loadedStyles)
        setSelectedId((current) => current ?? loadedClips[0]?.id ?? null)
      })
      .catch((err) => setError(err as Error))

    void api
      .getPodcast(jobId)
      .then(setPodcast)
      .catch(() => undefined)
  }, [jobId])

  const selected = useMemo(
    () => clips.find((clip) => clip.id === selectedId) ?? null,
    [clips, selectedId],
  )

  useEffect(() => {
    if (!selected) return
    setWordsDirty(false)
    setPlayhead(selected.start_s)
    api
      .getClipWords(selected.id)
      .then(setWords)
      .catch(() => setWords([]))
    // 404 is expected for audio-only sources and jobs that never reframed; the
    // player falls back to a centre crop, matching what the renderer does.
    api
      .getCropPath(selected.id)
      .then(setCropPath)
      .catch(() => setCropPath(null))
  }, [selected?.id])

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

  const saveLayout = async (layout: Clip['layout']) => {
    if (!selected) return
    setSavingLayout(true)
    try {
      patchClip(await api.patchLayout(selected.id, layout))
    } catch (err) {
      setError(err as Error)
    } finally {
      setSavingLayout(false)
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
    const targets = clips.filter((clip) => clip.status === 'kept')
    for (const clip of targets) await exportClip(clip)
  }

  const makePodcast = async () => {
    if (!jobId || (!podcastRemoveSilences && !podcastRemoveBoring)) return

    setPodcastBusy(true)
    setPodcastProgress(0)
    setPodcastMessage('Preparing podcast edit')
    setError(null)
    try {
      const result = await api.makePodcast(
        jobId,
        {
          remove_silences: podcastRemoveSilences,
          remove_boring_sections: podcastRemoveBoring,
          silence_threshold_s: 1.5,
          silence_keep_s: 0.25,
        },
        (event) => {
          if (event.type === 'status' && event.message) setPodcastMessage(event.message)
          if (event.type === 'progress' && event.progress !== undefined) {
            setPodcastProgress(Math.max(0, Math.min(1, event.progress)))
          }
        },
      )
      setPodcast(result)
      setPodcastProgress(1)
      setPodcastMessage('Podcast ready')
    } catch (err) {
      setError(err as Error)
      setPodcastMessage('')
      setPodcastProgress(null)
    } finally {
      setPodcastBusy(false)
    }
  }

  const keptCount = clips.filter((clip) => clip.status === 'kept').length
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

        <div className="flex items-baseline gap-6">
          <span className="numeric text-xs text-ink-500">
            {clips.length} clips · {keptCount} kept
          </span>
          <button onClick={exportKept} disabled={keptCount === 0} className="btn btn-primary">
            Export kept
          </button>
        </div>
      </div>

      {error && (
        <div className="mt-6 max-w-3xl">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
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
                  onLayoutSave={(layout) => void saveLayout(layout)}
                  captionsEnabled={selected.burn_captions}
                  cuts={selected.cuts}
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

                <div className="border-t border-ink-800 pt-6">
                  <p className="eyebrow">Podcast continuation</p>
                  <h3 className="mt-2 font-display text-xl text-ink-100">
                    Make this stream into a podcast
                  </h3>
                  <p className="mt-2 text-sm leading-relaxed text-ink-400">
                    AutoClip sends the saved transcript back through {job.provider || 'the active AI'}
                    {' '}to find repetitive or low-information spoken sections, then combines those
                    edits with measured silence gaps and renders a separate MP3 from the original
                    source audio.
                  </p>

                  <div className="mt-4 space-y-3">
                    <label className="flex items-start gap-3 text-sm text-ink-200">
                      <input
                        type="checkbox"
                        checked={podcastRemoveBoring}
                        disabled={podcastBusy}
                        onChange={(event) => setPodcastRemoveBoring(event.target.checked)}
                        className="mt-0.5 size-4 accent-sodium-500"
                      />
                      <span>
                        Remove boring / repetitive spoken sections with AI
                        <span className="mt-0.5 block text-xs text-ink-500">
                          Conservative by design: stories, jokes, opinions, explanations, and
                          context needed for later payoffs are kept.
                        </span>
                      </span>
                    </label>

                    <label className="flex items-start gap-3 text-sm text-ink-200">
                      <input
                        type="checkbox"
                        checked={podcastRemoveSilences}
                        disabled={podcastBusy}
                        onChange={(event) => setPodcastRemoveSilences(event.target.checked)}
                        className="mt-0.5 size-4 accent-sodium-500"
                      />
                      <span>
                        Shorten long silent gaps
                        <span className="mt-0.5 block text-xs text-ink-500">
                          Gaps of 1.5 seconds or longer are tightened while keeping a short natural
                          pause around each edit.
                        </span>
                      </span>
                    </label>
                  </div>

                  {podcastBusy && (
                    <div className="mt-5 border border-ink-800 bg-ink-850/40 p-3">
                      <div className="flex items-baseline justify-between gap-3 text-xs">
                        <span className="text-ink-300">{podcastMessage || 'Working…'}</span>
                        <span className="numeric text-ink-500">
                          {Math.round((podcastProgress ?? 0) * 100)}%
                        </span>
                      </div>
                      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-ink-800">
                        <div
                          className="h-full bg-sodium-500 transition-[width] duration-200"
                          style={{ width: `${Math.round((podcastProgress ?? 0) * 100)}%` }}
                        />
                      </div>
                    </div>
                  )}

                  <button
                    type="button"
                    onClick={() => void makePodcast()}
                    disabled={
                      podcastBusy ||
                      !job.source?.has_audio ||
                      (!podcastRemoveSilences && !podcastRemoveBoring)
                    }
                    className="btn btn-primary mt-5 w-full"
                  >
                    {podcastBusy
                      ? 'Making podcast…'
                      : podcast
                        ? 'Regenerate podcast'
                        : 'Make into podcast'}
                  </button>

                  {!job.source?.has_audio && (
                    <p className="mt-2 text-xs text-signal-bad">
                      This source has no audio track, so a podcast cannot be generated.
                    </p>
                  )}

                  {podcast && (
                    <div className="mt-4 border border-ink-800 bg-ink-850/40 p-4">
                      <div className="flex flex-wrap items-baseline justify-between gap-3">
                        <div>
                          <p className="text-sm font-medium text-ink-100">Podcast ready</p>
                          <p className="mt-1 text-xs text-ink-500">
                            {formatDuration(podcast.source_duration_s)} →{' '}
                            {formatDuration(podcast.output_duration_s)} · removed{' '}
                            {formatDuration(podcast.removed_duration_s)}
                          </p>
                        </div>
                        <a
                          href={podcast.download_url}
                          download
                          className="text-sm text-sodium-500 underline underline-offset-4"
                        >
                          Download MP3
                        </a>
                      </div>
                      <p className="mt-3 text-xs leading-relaxed text-ink-500">
                        {podcast.total_cut_count} edits · {podcast.boring_cut_count} AI-assisted ·{' '}
                        {podcast.silence_cut_count} silence · {formatBytes(podcast.size_bytes)}
                        {podcast.model ? ` · ${podcast.provider}/${podcast.model}` : ''}
                      </p>
                    </div>
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
