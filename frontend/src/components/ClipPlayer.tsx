import { useCallback, useEffect, useRef, useState } from 'react'

import {
  formatTimecode,
  type CaptionStyle,
  type CropPath,
  type CutRange,
  type LayoutRect,
  type LayoutRegion,
  type ManualLayout,
  type Word,
} from '../api'
import { LayoutEditor } from './LayoutEditor'

const VOLUME_KEY = 'autoclip.volume'
const ASS_REFERENCE_HEIGHT = 1920

/** Output shapes, matching autoclip.pipeline.export.RATIOS. */
const ASPECTS: Record<string, [number, number]> = {
  '9:16': [9, 16],
  '1:1': [1, 1],
  '16:9': [16, 9],
}

const NORMAL_HEIGHT_VH = 62
const EXPANDED_HEIGHT_VH = 82
const FULLSCREEN_HEIGHT_VH = 90

function readStoredVolume(): number {
  const stored = Number(window.localStorage.getItem(VOLUME_KEY))
  return Number.isFinite(stored) && stored > 0 && stored <= 1 ? stored : 1
}

/**
 * Clip-only preview with export-aware framing, captions, cuts and seeking.
 *
 * Caption measurements are derived from the rendered preview frame using the
 * same ratios the ASS exporter uses. That keeps font size, margins, outlines,
 * active-word scaling and bundled fonts proportional to the final render.
 */
export function ClipPlayer({
  src,
  startS,
  endS,
  words,
  style,
  ratio,
  cropPath,
  layout = null,
  sourceWidth = null,
  sourceHeight = null,
  layoutSaving = false,
  onLayoutSave,
  captionsEnabled = true,
  cuts = [],
  onTimeChange,
}: {
  src: string
  startS: number
  endS: number
  words: Word[]
  style: CaptionStyle | undefined
  ratio: string
  cropPath?: CropPath | null
  layout?: ManualLayout | null
  sourceWidth?: number | null
  sourceHeight?: number | null
  layoutSaving?: boolean
  onLayoutSave?: (layout: ManualLayout | null) => void
  captionsEnabled?: boolean
  cuts?: CutRange[]
  onTimeChange?: (time: number) => void
}) {
  const shell = useRef<HTMLDivElement>(null)
  const frame = useRef<HTMLDivElement>(null)
  const video = useRef<HTMLVideoElement>(null)
  const [playing, setPlaying] = useState(false)
  const [time, setTime] = useState(startS)
  const [mediaReady, setMediaReady] = useState(false)
  const [mediaError, setMediaError] = useState<string | null>(null)
  const [audioOnly, setAudioOnly] = useState(false)
  const [volume, setVolume] = useState(readStoredVolume)
  const [muted, setMuted] = useState(false)
  const [audioCheck, setAudioCheck] = useState<AudioCheck | null>(null)
  const [checking, setChecking] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const [frameHeight, setFrameHeight] = useState(0)
  const [previewLayout, setPreviewLayout] = useState<ManualLayout | null>(layout)

  const sortedCuts = [...cuts].sort((a, b) => a.start_s - b.start_s)
  const duration = Math.max(0.01, effectiveDuration(startS, endS, sortedCuts))
  const elapsed = Math.min(duration, effectiveElapsed(time, startS, sortedCuts))
  const sourceElapsed = Math.max(0, time - startS)
  const previewWords = retimeWordsForPreview(words, startS, sortedCuts)

  useEffect(() => setPreviewLayout(layout), [layout, startS, endS])

  useEffect(() => {
    if (!expanded && !fullscreen) setPreviewLayout(layout)
  }, [expanded, fullscreen, layout])

  const [aspectW, aspectH] = ASPECTS[ratio] ?? ASPECTS['9:16']
  const heightVh = fullscreen
    ? FULLSCREEN_HEIGHT_VH
    : expanded
      ? EXPANDED_HEIGHT_VH
      : NORMAL_HEIGHT_VH
  const maxWidth = `${((heightVh * aspectW) / aspectH).toFixed(3)}vh`

  useEffect(() => {
    const element = frame.current
    if (!element) return

    const update = () => setFrameHeight(element.getBoundingClientRect().height)
    update()
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [ratio, expanded, fullscreen])

  useEffect(() => {
    const onFullscreenChange = () => {
      setFullscreen(document.fullscreenElement === shell.current)
    }
    document.addEventListener('fullscreenchange', onFullscreenChange)
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange)
  }, [])

  useEffect(() => {
    if (!expanded || fullscreen) return

    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setExpanded(false)
    }
    window.addEventListener('keydown', close)
    return () => {
      document.body.style.overflow = previous
      window.removeEventListener('keydown', close)
    }
  }, [expanded, fullscreen])

  const runAudioCheck = async () => {
    const element = video.current
    if (!element) return
    setChecking(true)
    setAudioCheck(null)
    try {
      setAudioCheck(await measureOutputLevel(element))
    } catch (error) {
      setAudioCheck({ ok: false, peakDb: null, detail: String(error) })
    } finally {
      setChecking(false)
      setPlaying(!element.paused)
    }
  }

  useEffect(() => {
    const element = video.current
    if (!element) return
    element.volume = volume
    element.muted = muted
    window.localStorage.setItem(VOLUME_KEY, String(volume))
  }, [volume, muted])

  useEffect(() => {
    const element = video.current
    if (!element) return
    setMediaReady(false)
    setMediaError(null)
    setAudioOnly(false)
    setTime(startS)
    onTimeChange?.(startS)
    setPlaying(false)
    element.pause()

    if (element.readyState >= element.HAVE_METADATA) {
      try {
        element.currentTime = startS
      } catch {
        // loadedmetadata will perform the seek once the browser accepts it.
      }
    }
  }, [src, startS, onTimeChange])

  const onTimeUpdate = useCallback(() => {
    const element = video.current
    if (!element) return

    const cut = sortedCuts.find(
      (range) => element.currentTime >= range.start_s && element.currentTime < range.end_s,
    )
    if (cut) element.currentTime = Math.min(cut.end_s, endS)

    if (element.currentTime >= endS) {
      element.pause()
      element.currentTime = startS
      setPlaying(false)
      setTime(startS)
      onTimeChange?.(startS)
      return
    }

    setTime(element.currentTime)
    onTimeChange?.(element.currentTime)
  }, [sortedCuts, endS, startS, onTimeChange])

  const seekToEditedTime = (editedTime: number) => {
    const element = video.current
    if (!element) return

    const target = sourceTimeForEdited(
      Math.max(0, Math.min(duration, editedTime)),
      startS,
      endS,
      sortedCuts,
    )
    element.currentTime = target
    setTime(target)
    onTimeChange?.(target)
  }

  const toggle = () => {
    const element = video.current
    if (!element) return
    if (element.paused) {
      if (element.currentTime < startS || element.currentTime >= endS) {
        element.currentTime = startS
      }
      const cut = sortedCuts.find(
        (range) => element.currentTime >= range.start_s && element.currentTime < range.end_s,
      )
      if (cut) element.currentTime = cut.end_s
      setMediaError(null)
      void element
        .play()
        .then(() => setPlaying(true))
        .catch((error) => {
          setPlaying(false)
          setMediaError(playbackErrorMessage(element, error))
        })
    } else {
      element.pause()
      setPlaying(false)
    }
  }

  const toggleFullscreen = async () => {
    if (!shell.current) return
    if (document.fullscreenElement === shell.current) {
      await document.exitFullscreen()
    } else {
      await shell.current.requestFullscreen()
    }
  }

  const resolvedSourceWidth = sourceWidth ?? cropPath?.source_width ?? 0
  const resolvedSourceHeight = sourceHeight ?? cropPath?.source_height ?? 0
  const manualLayout =
    previewLayout &&
    (ratio === '9:16' || ratio === '1:1') &&
    resolvedSourceWidth > 0 &&
    resolvedSourceHeight > 0
      ? previewLayout
      : null
  const layoutEditorVisible =
    (expanded || fullscreen) &&
    (ratio === '9:16' || ratio === '1:1') &&
    onLayoutSave !== undefined
  const cropStyle = manualLayout
    ? manualBaseWindowStyle(
        manualLayout,
        resolvedSourceWidth,
        resolvedSourceHeight,
        aspectW,
        aspectH,
      )
    : cropWindowStyle(cropPath, sourceElapsed)
  const fitFrame =
    manualLayout === null && (activeCropSegment(cropPath, sourceElapsed)?.fit ?? false)

  return (
    <div
      ref={shell}
      className={[
        'bg-ink-900',
        fullscreen
          ? 'h-screen w-screen overflow-auto p-4'
          : expanded
            ? 'fixed inset-3 z-50 overflow-auto border border-ink-700 p-4 shadow-2xl'
            : 'mx-auto w-full',
      ].join(' ')}
    >
      <div
        className={
          layoutEditorVisible
            ? 'mx-auto grid w-full max-w-[96rem] gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(24rem,34rem)]'
            : 'mx-auto w-full'
        }
      >
        <div className="mx-auto flex w-full flex-col items-stretch" style={{ maxWidth }}>
        <div className="mb-2 flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={() => setExpanded((current) => !current)}
            className="btn btn-quiet"
          >
            {expanded
              ? 'Collapse preview'
              : ratio === '9:16' || ratio === '1:1'
                ? 'Expand + edit layout'
                : 'Expand preview'}
          </button>
          <button type="button" onClick={() => void toggleFullscreen()} className="btn btn-quiet">
            {fullscreen ? 'Exit full screen' : 'Full screen'}
          </button>
        </div>

        <div
          ref={frame}
          className="relative w-full overflow-hidden bg-ink-850"
          style={{ aspectRatio: `${aspectW} / ${aspectH}` }}
          onClick={toggle}
          role="button"
          tabIndex={0}
          aria-label={playing ? 'Pause' : 'Play'}
          onKeyDown={(event) => {
            if (event.key === ' ' || event.key === 'Enter') {
              event.preventDefault()
              toggle()
            } else if (event.key === 'ArrowLeft') {
              event.preventDefault()
              seekToEditedTime(elapsed - 5)
            } else if (event.key === 'ArrowRight') {
              event.preventDefault()
              seekToEditedTime(elapsed + 5)
            }
          }}
        >
          <video
            ref={video}
            src={src}
            className={
              cropStyle
                ? 'absolute max-w-none'
                : fitFrame
                  ? 'size-full object-contain'
                  : 'size-full object-cover'
            }
            style={cropStyle ?? undefined}
            onLoadedMetadata={(event) => {
              const element = event.currentTarget
              setAudioOnly(element.videoWidth === 0 || element.videoHeight === 0)
              setMediaError(null)
              try {
                element.currentTime = startS
              } catch (error) {
                setMediaError(playbackErrorMessage(element, error))
              }
            }}
            onCanPlay={() => {
              setMediaReady(true)
              setMediaError(null)
            }}
            onError={(event) => {
              setMediaReady(false)
              setPlaying(false)
              setMediaError(playbackErrorMessage(event.currentTarget))
            }}
            onTimeUpdate={onTimeUpdate}
            preload="auto"
            playsInline
          />

          {manualLayout?.overlays.map((region) => (
            <LayoutOverlayVideo
              key={region.id}
              src={src}
              region={region}
              time={time}
              playing={playing}
            />
          ))}

          {captionsEnabled && (
            <CaptionOverlay
              words={previewWords}
              time={elapsed}
              style={style}
              frameHeight={frameHeight}
            />
          )}

          {!playing && !mediaError && (
            <div className="pointer-events-none absolute inset-0 grid place-items-center">
              <span className="grid size-16 place-items-center rounded-full bg-ink-900/70 pl-1 text-2xl text-ink-100 backdrop-blur-[2px]">
                ▶
              </span>
            </div>
          )}

          {!mediaReady && !mediaError && (
            <span className="pointer-events-none absolute left-3 top-3 bg-ink-900/80 px-2 py-1 text-xs text-ink-400">
              Loading preview…
            </span>
          )}

          {audioOnly && !mediaError && (
            <div className="pointer-events-none absolute inset-x-4 bottom-4 bg-ink-900/85 p-3 text-center text-xs leading-relaxed text-ink-300">
              This source has audio but no decodable video frames, so the visual preview is blank.
              Audio playback and clip timing can still work.
            </div>
          )}

          {mediaError && (
            <div className="absolute inset-x-4 bottom-4 border border-signal-bad/50 bg-ink-900/95 p-3 text-xs leading-relaxed text-signal-bad">
              <strong className="block text-ink-100">Preview media could not play.</strong>
              <span className="mt-1 block">{mediaError}</span>
              <span className="mt-1 block text-ink-500">
                The source may be missing, cached from before the storage move, or encoded with a
                browser-unsupported codec.
              </span>
            </div>
          )}
        </div>

        <div className="mt-3">
          <input
            type="range"
            min={0}
            max={duration}
            step={0.01}
            value={elapsed}
            onChange={(event) => seekToEditedTime(Number(event.target.value))}
            aria-label="Seek within edited clip"
            className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-ink-700 accent-sodium-500"
          />
          <div className="mt-1 flex justify-between text-xs text-ink-500">
            <span className="numeric">{formatTimecode(elapsed)}</span>
            <span className="numeric">{formatTimecode(duration)}</span>
          </div>
        </div>

        <div className="mt-2 flex w-full flex-wrap items-center gap-x-4 gap-y-2">
          <button onClick={toggle} className="btn btn-quiet -ml-1 w-14 justify-start">
            {playing ? 'Pause' : 'Play'}
          </button>

          <VolumeControl
            volume={volume}
            muted={muted}
            onVolume={(next) => {
              setVolume(next)
              if (next > 0) setMuted(false)
            }}
            onToggleMute={() => setMuted((current) => !current)}
          />

          <span className="numeric ml-auto text-xs text-ink-500">
            {formatTimecode(elapsed)} / {formatTimecode(duration)}
          </span>
        </div>

        {(muted || volume === 0) && (
          <p className="mt-2 text-xs text-sodium-500">
            Audio is muted — click the speaker to unmute.
          </p>
        )}

        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
          <button onClick={runAudioCheck} disabled={checking} className="btn btn-quiet -ml-1">
            {checking ? 'Listening…' : 'Test audio'}
          </button>
          <span className="text-xs text-ink-600">← / → seek 5 seconds</span>
        </div>

        {audioCheck && (
          <p
            className={`mt-1 max-w-prose text-xs leading-relaxed ${
              audioCheck.ok ? 'text-ink-400' : 'text-sodium-500'
            }`}
          >
            {audioCheck.detail}
          </p>
        )}
        </div>

        {layoutEditorVisible && (
          <div className="min-w-0 xl:max-h-[calc(100vh-2rem)] xl:overflow-y-auto xl:pr-1">
            <LayoutEditor
              layout={layout}
              ratio={ratio}
              src={src}
              currentTime={time}
              sourceWidth={resolvedSourceWidth || null}
              sourceHeight={resolvedSourceHeight || null}
              saving={layoutSaving}
              onSave={(next) => {
                setPreviewLayout(next)
                onLayoutSave?.(next)
              }}
              onPreviewChange={setPreviewLayout}
            />
          </div>
        )}
      </div>
    </div>
  )
}

function retimeWordsForPreview(words: Word[], startS: number, cuts: CutRange[]): Word[] {
  return words.flatMap((word) => {
    if (cuts.some((cut) => word.start < cut.end_s && word.end > cut.start_s)) return []

    const shift = cuts.reduce(
      (total, cut) => total + (cut.end_s <= word.start ? cut.end_s - cut.start_s : 0),
      0,
    )
    return [
      {
        ...word,
        start: word.start - startS - shift,
        end: word.end - startS - shift,
      },
    ]
  })
}

function effectiveElapsed(time: number, startS: number, cuts: CutRange[]): number {
  let elapsed = Math.max(0, time - startS)
  for (const cut of cuts) {
    if (time >= cut.end_s) elapsed -= cut.end_s - cut.start_s
    else if (time > cut.start_s) elapsed -= time - cut.start_s
  }
  return Math.max(0, elapsed)
}

function effectiveDuration(startS: number, endS: number, cuts: CutRange[]): number {
  const removed = cuts.reduce((total, cut) => {
    const start = Math.max(startS, cut.start_s)
    const end = Math.min(endS, cut.end_s)
    return total + Math.max(0, end - start)
  }, 0)
  return Math.max(0, endS - startS - removed)
}

function sourceTimeForEdited(
  editedTime: number,
  startS: number,
  endS: number,
  cuts: CutRange[],
): number {
  let source = startS + editedTime
  let removedBefore = 0

  for (const cut of cuts) {
    const cutStart = Math.max(startS, cut.start_s)
    const cutEnd = Math.min(endS, cut.end_s)
    if (cutEnd <= cutStart) continue

    const editedCutStart = cutStart - startS - removedBefore
    if (editedTime < editedCutStart) break

    const removed = cutEnd - cutStart
    source += removed
    removedBefore += removed
  }

  return Math.max(startS, Math.min(endS - 0.001, source))
}

function playbackErrorMessage(
  element: HTMLVideoElement,
  cause?: unknown,
): string {
  const mediaError = element.error
  if (mediaError) {
    const labels: Record<number, string> = {
      1: 'Playback was aborted.',
      2: 'The browser could not load the media from AutoClip.',
      3: 'The browser could not decode this media.',
      4: 'The media format or codec is not supported by this browser.',
    }
    const detail = labels[mediaError.code] ?? `Media error code ${mediaError.code}.`
    const nativeMessage = 'message' in mediaError ? mediaError.message : ''
    return nativeMessage ? `${detail} ${nativeMessage}` : detail
  }

  if (cause instanceof Error && cause.message) return cause.message
  if (cause) return String(cause)
  return 'The browser reported an unknown media playback error.'
}

interface AudioCheck {
  ok: boolean
  peakDb: number | null
  detail: string
}

async function measureOutputLevel(element: HTMLVideoElement): Promise<AudioCheck> {
  const capture =
    (element as HTMLVideoElement & { captureStream?: () => MediaStream }).captureStream ??
    (element as HTMLVideoElement & { mozCaptureStream?: () => MediaStream }).mozCaptureStream

  if (!capture) {
    return {
      ok: false,
      peakDb: null,
      detail: "This browser can't measure audio output. The clip player can still seek and play normally.",
    }
  }

  const wasPaused = element.paused
  if (wasPaused) await element.play()

  const stream = capture.call(element)
  const tracks = stream.getAudioTracks()
  if (tracks.length === 0) {
    if (wasPaused) element.pause()
    return {
      ok: false,
      peakDb: null,
      detail: 'This video exposes no audio track at all — that is a problem in AutoClip.',
    }
  }

  const context = new AudioContext()
  const analyser = context.createAnalyser()
  analyser.fftSize = 2048
  context.createMediaStreamSource(stream).connect(analyser)

  const samples = new Float32Array(analyser.fftSize)
  let peak = 0
  for (let i = 0; i < 24; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 50))
    analyser.getFloatTimeDomainData(samples)
    for (const sample of samples) peak = Math.max(peak, Math.abs(sample))
  }

  await context.close()
  if (wasPaused) element.pause()

  if (peak <= 0.005) {
    return {
      ok: false,
      peakDb: null,
      detail:
        'No signal is leaving the player. Either the clip really is silent, or the ' +
        'player is muted — check the speaker icon above.',
    }
  }

  const peakDb = 20 * Math.log10(peak)
  return {
    ok: true,
    peakDb,
    detail:
      `Audio is leaving the player at ${peakDb.toFixed(1)} dBFS — the app is producing sound. ` +
      'If you still hear nothing, it is between the browser and your speakers: right-click ' +
      'this tab and check for "Unmute site", then check Windows Volume Mixer and your ' +
      'output device.',
  }
}

function manualBaseWindowStyle(
  layout: ManualLayout,
  sourceWidth: number,
  sourceHeight: number,
  aspectW: number,
  aspectH: number,
): React.CSSProperties {
  const targetRatio = aspectW / aspectH
  const sourceRatio = sourceWidth / sourceHeight

  let cropWidth = sourceWidth
  let cropHeight = sourceHeight
  if (sourceRatio >= targetRatio) {
    cropWidth = sourceHeight * targetRatio
  } else {
    cropHeight = sourceWidth / targetRatio
  }

  const x = (sourceWidth - cropWidth) * layout.base_center_x
  const y = (sourceHeight - cropHeight) * layout.base_center_y

  return {
    width: `${(sourceWidth / cropWidth) * 100}%`,
    height: `${(sourceHeight / cropHeight) * 100}%`,
    left: `${(-x / cropWidth) * 100}%`,
    top: `${(-y / cropHeight) * 100}%`,
  }
}

function LayoutOverlayVideo({
  src,
  region,
  time,
  playing,
}: {
  src: string
  region: LayoutRegion
  time: number
  playing: boolean
}) {
  const overlay = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    const element = overlay.current
    if (!element) return

    if (Math.abs(element.currentTime - time) > 0.08) {
      element.currentTime = time
    }

    if (playing) {
      void element.play().catch(() => undefined)
    } else {
      element.pause()
    }
  }, [time, playing])

  return (
    <div
      className="pointer-events-none absolute overflow-hidden"
      style={{
        left: `${region.destination.x * 100}%`,
        top: `${region.destination.y * 100}%`,
        width: `${region.destination.width * 100}%`,
        height: `${region.destination.height * 100}%`,
      }}
    >
      <video
        ref={overlay}
        src={src}
        muted
        playsInline
        preload="auto"
        draggable={false}
        onDragStart={(event) => event.preventDefault()}
        className="pointer-events-none absolute max-w-none select-none"
        style={sourceRectWindowStyle(region.source)}
      />
    </div>
  )
}

function sourceRectWindowStyle(rect: LayoutRect): React.CSSProperties {
  return {
    width: `${100 / rect.width}%`,
    height: `${100 / rect.height}%`,
    maxWidth: 'none',
    maxHeight: 'none',
    objectFit: 'fill',
    left: `${(-rect.x / rect.width) * 100}%`,
    top: `${(-rect.y / rect.height) * 100}%`,
  }
}

function activeCropSegment(cropPath: CropPath | null | undefined, elapsed: number) {
  if (!cropPath || cropPath.segments.length === 0) return null
  return (
    cropPath.segments.find((segment) => elapsed >= segment.start_s && elapsed < segment.end_s) ??
    cropPath.segments[cropPath.segments.length - 1]
  )
}

function cropWindowStyle(
  cropPath: CropPath | null | undefined,
  elapsed: number,
): React.CSSProperties | null {
  const segment = activeCropSegment(cropPath, elapsed)
  if (!cropPath || !segment || segment.fit) return null

  const { x, y } = interpolate(segment.keyframes, elapsed)
  const { source_width: sourceW, source_height: sourceH } = cropPath

  return {
    width: `${(sourceW / segment.width) * 100}%`,
    height: `${(sourceH / segment.height) * 100}%`,
    left: `${(-x / segment.width) * 100}%`,
    top: `${(-y / segment.height) * 100}%`,
  }
}

function interpolate(
  keyframes: { t: number; x: number; y: number }[],
  t: number,
): { x: number; y: number } {
  if (keyframes.length === 0) return { x: 0, y: 0 }
  if (keyframes.length === 1) return { x: keyframes[0].x, y: keyframes[0].y }

  if (t <= keyframes[0].t) return { x: keyframes[0].x, y: keyframes[0].y }
  const last = keyframes[keyframes.length - 1]
  if (t >= last.t) return { x: last.x, y: last.y }

  for (let i = 0; i < keyframes.length - 1; i += 1) {
    const a = keyframes[i]
    const b = keyframes[i + 1]
    if (t >= a.t && t <= b.t) {
      const span = b.t - a.t
      const ratio = span > 0 ? (t - a.t) / span : 0
      return { x: a.x + (b.x - a.x) * ratio, y: a.y + (b.y - a.y) * ratio }
    }
  }

  return { x: last.x, y: last.y }
}

function VolumeControl({
  volume,
  muted,
  onVolume,
  onToggleMute,
}: {
  volume: number
  muted: boolean
  onVolume: (value: number) => void
  onToggleMute: () => void
}) {
  const silent = muted || volume === 0

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={onToggleMute}
        className={`btn btn-quiet px-1 ${silent ? 'text-sodium-500' : ''}`}
        aria-label={silent ? 'Unmute' : 'Mute'}
        title={silent ? 'Unmute' : 'Mute'}
      >
        <SpeakerIcon silent={silent} level={volume} />
      </button>
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={silent ? 0 : volume}
        onChange={(event) => onVolume(Number(event.target.value))}
        aria-label="Volume"
        className="h-1 w-20 cursor-pointer appearance-none rounded-full bg-ink-700 accent-sodium-500"
      />
    </div>
  )
}

function SpeakerIcon({ silent, level }: { silent: boolean; level: number }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M11 5 6 9H3v6h3l5 4V5Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      {silent ? (
        <path
          d="m16 9 5 6m0-6-5 6"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      ) : (
        <>
          <path
            d="M15.5 9.5a3.5 3.5 0 0 1 0 5"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
          {level > 0.5 && (
            <path
              d="M18.5 7a7 7 0 0 1 0 10"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          )}
        </>
      )}
    </svg>
  )
}

function CaptionOverlay({
  words,
  time,
  style,
  frameHeight,
}: {
  words: Word[]
  time: number
  style: CaptionStyle | undefined
  frameHeight: number
}) {
  if (!style || words.length === 0 || frameHeight <= 0) return null

  const groups = groupWords(words, style.preview.maxWords)
  const group = groups.find(
    (candidate) => time >= candidate[0].start && time <= candidate[candidate.length - 1].end,
  )
  if (!group) return null

  const {
    primary,
    accent,
    outline,
    outlineWidth,
    shadow,
    bold,
    allCaps,
    boxed,
    boxColour,
    boxAlpha,
    marginRatio,
    sizeRatio,
    animation,
    scalePercent,
    font,
  } = style.preview

  const activeIndex = group.findIndex((word) => time >= word.start && time <= word.end)
  if (animation === 'scale' && accent && activeIndex < 0) return null

  const fontSize = frameHeight * sizeRatio
  const outlinePx = outlineWidth * (frameHeight / ASS_REFERENCE_HEIGHT)
  const shadowPx = shadow * (frameHeight / ASS_REFERENCE_HEIGHT)

  return (
    <div
      className="pointer-events-none absolute inset-x-0 flex justify-center"
      style={{
        bottom: `${marginRatio * 100}%`,
        paddingInline: '3%',
      }}
    >
      <p
        className="m-0 max-w-full text-center"
        style={{
          fontFamily: `'${font}', sans-serif`,
          fontSize: `${fontSize}px`,
          fontWeight: font === 'Anton' ? 400 : bold ? 700 : 400,
          lineHeight: 1.08,
          textTransform: allCaps ? 'uppercase' : 'none',
          color: primary,
          WebkitTextStroke: boxed ? undefined : `${outlinePx}px ${outline}`,
          paintOrder: 'stroke fill',
          textShadow:
            !boxed && shadowPx > 0
              ? `${shadowPx}px ${shadowPx}px 0 rgba(0,0,0,0.9)`
              : undefined,
          background: boxed ? assBoxColour(boxColour, boxAlpha) : undefined,
          padding: boxed ? '0.14em 0.38em' : undefined,
        }}
      >
        {group.map((word, index) => {
          const text = allCaps ? word.text.toUpperCase() : word.text
          const isActive = index === activeIndex
          const wordStyle: React.CSSProperties = {
            display: 'inline-block',
            marginInline: '0.14em',
          }

          if (animation === 'scale' && accent && isActive) {
            wordStyle.color = accent
            wordStyle.transform = `scale(${scalePercent / 100})`
            wordStyle.transformOrigin = 'center'
          } else if (animation === 'karaoke' && accent) {
            Object.assign(wordStyle, karaokeWordStyle(word, time, primary, accent))
          }

          return (
            <span key={`${word.start}-${index}`} style={wordStyle}>
              {text}
            </span>
          )
        })}
      </p>
    </div>
  )
}

function karaokeWordStyle(
  word: Word,
  time: number,
  primary: string,
  secondary: string,
): React.CSSProperties {
  if (time <= word.start) return { color: secondary }
  if (time >= word.end) return { color: primary }

  const progress = Math.max(0, Math.min(1, (time - word.start) / Math.max(0.001, word.end - word.start)))
  const percent = (progress * 100).toFixed(1)
  return {
    color: 'transparent',
    backgroundImage: `linear-gradient(90deg, ${primary} 0 ${percent}%, ${secondary} ${percent}% 100%)`,
    backgroundClip: 'text',
    WebkitBackgroundClip: 'text',
  }
}

function assBoxColour(hex: string, assAlpha: number): string {
  const value = hex.replace('#', '')
  if (value.length !== 6) return hex
  const red = Number.parseInt(value.slice(0, 2), 16)
  const green = Number.parseInt(value.slice(2, 4), 16)
  const blue = Number.parseInt(value.slice(4, 6), 16)
  const opacity = Math.max(0, Math.min(1, (255 - assAlpha) / 255))
  return `rgba(${red}, ${green}, ${blue}, ${opacity})`
}

function groupWords(words: Word[], maxWords: number): Word[][] {
  const groups: Word[][] = []
  let current: Word[] = []

  for (const [index, word] of words.entries()) {
    if (current.length > 0) {
      const gap = word.start - current[current.length - 1].end
      if (gap > 0.4 || current.length >= maxWords) {
        groups.push(current)
        current = []
      }
    }

    current.push(word)

    const endsSentence = /[.!?…]+["'”’)\]]*$/.test(word.text.trim())
    if (endsSentence && index !== words.length - 1) {
      groups.push(current)
      current = []
    }
  }

  if (current.length > 0) groups.push(current)
  return groups
}
