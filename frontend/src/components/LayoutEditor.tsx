import { useEffect, useRef, useState } from 'react'

import type { LayoutRect, LayoutRegion, ManualLayout } from '../api'

const EMPTY_LAYOUT: ManualLayout = {
  base_center_x: 0.5,
  base_center_y: 0.5,
  overlays: [],
}

const MIN_REGION_SIZE = 0.03
const MIN_DESTINATION_SIZE = 0.08

export function LayoutEditor({
  layout,
  ratio,
  src,
  currentTime,
  sourceWidth,
  sourceHeight,
  saving,
  onSave,
  onPreviewChange,
}: {
  layout: ManualLayout | null
  ratio: string
  src: string
  currentTime: number
  sourceWidth: number | null
  sourceHeight: number | null
  saving: boolean
  onSave: (layout: ManualLayout | null) => void
  onPreviewChange: (layout: ManualLayout | null) => void
}) {
  const [draft, setDraft] = useState<ManualLayout | null>(layout)
  const [dirty, setDirty] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(
    layout?.overlays[0]?.id ?? null,
  )
  const [selectingSourceFor, setSelectingSourceFor] = useState<'new' | string | null>(null)
  const [selection, setSelection] = useState<LayoutRect | null>(null)

  const sourceStage = useRef<HTMLDivElement>(null)
  const outputStage = useRef<HTMLDivElement>(null)
  const sourceDrag = useRef<SourceDrag | null>(null)
  const outputDrag = useRef<OutputDrag | null>(null)

  const supported = ratio === '9:16' || ratio === '1:1'
  const dimensionsReady = Boolean(sourceWidth && sourceHeight)
  const sourceAspect =
    sourceWidth && sourceHeight && sourceHeight > 0 ? sourceWidth / sourceHeight : 16 / 9
  const outputAspect = ratio === '1:1' ? 1 : 9 / 16

  useEffect(() => {
    setDraft(layout)
    setDirty(false)
    setSelectedId(layout?.overlays[0]?.id ?? null)
    setSelectingSourceFor(null)
    setSelection(null)
    onPreviewChange(layout)
  }, [layout, onPreviewChange])

  const update = (next: ManualLayout | null) => {
    setDraft(next)
    setDirty(true)
    onPreviewChange(next)
  }

  const enable = () => {
    const next = { ...EMPTY_LAYOUT, overlays: [] }
    update(next)
    setSelectedId(null)
  }

  const updateRegion = (id: string, updater: (region: LayoutRegion) => LayoutRegion) => {
    if (!draft) return
    update({
      ...draft,
      overlays: draft.overlays.map((region) => (region.id === id ? updater(region) : region)),
    })
  }

  const removeRegion = (id: string) => {
    if (!draft) return
    const overlays = draft.overlays.filter((region) => region.id !== id)
    update({ ...draft, overlays })
    setSelectedId((current) => (current === id ? overlays[0]?.id ?? null : current))
  }

  const beginSourceSelection = (target: 'new' | string) => {
    setSelectingSourceFor(target)
    setSelection(null)
  }

  const startSourcePointer = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!sourceStage.current || !draft) return
    event.preventDefault()
    const point = normalizedPoint(event, sourceStage.current)

    if (selectingSourceFor) {
      event.currentTarget.setPointerCapture(event.pointerId)
      sourceDrag.current = {
        kind: 'select',
        pointerId: event.pointerId,
        start: point,
        captureElement: event.currentTarget,
      }
      setSelection({ x: point.x, y: point.y, width: 0, height: 0 })
      return
    }

    // Clicking the source outside a region moves the base crop frame there.
    const crop = baseCropRect(sourceAspect, outputAspect, draft)
    const centerX = clamp(point.x - crop.width / 2, 0, 1 - crop.width)
    const centerY = clamp(point.y - crop.height / 2, 0, 1 - crop.height)
    update({
      ...draft,
      base_center_x: crop.width >= 0.999 ? 0.5 : centerX / (1 - crop.width),
      base_center_y: crop.height >= 0.999 ? 0.5 : centerY / (1 - crop.height),
    })
  }

  const moveSourcePointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = sourceDrag.current
    if (!drag || !sourceStage.current || !draft) return
    const point = normalizedPoint(event, sourceStage.current)

    if (drag.kind === 'select') {
      setSelection(rectFromPoints(drag.start, point))
      return
    }

    const dx = point.x - drag.start.x
    const dy = point.y - drag.start.y
    const x = clamp(drag.initial.x + dx, 0, 1 - drag.initial.width)
    const y = clamp(drag.initial.y + dy, 0, 1 - drag.initial.height)
    update({
      ...draft,
      base_center_x:
        drag.initial.width >= 0.999 ? 0.5 : x / Math.max(0.001, 1 - drag.initial.width),
      base_center_y:
        drag.initial.height >= 0.999 ? 0.5 : y / Math.max(0.001, 1 - drag.initial.height),
    })
  }

  const endSourcePointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = sourceDrag.current
    const stage = sourceStage.current
    if (!drag) return
    sourceDrag.current = null

    if (drag.captureElement.hasPointerCapture(event.pointerId)) {
      drag.captureElement.releasePointerCapture(event.pointerId)
    }

    if (drag.kind !== 'select' || !draft || !selectingSourceFor || !stage) {
      setSelection(null)
      return
    }

    const finalSelection = rectFromPoints(
      drag.start,
      normalizedPoint(event, stage),
    )
    if (
      finalSelection.width < MIN_REGION_SIZE ||
      finalSelection.height < MIN_REGION_SIZE
    ) {
      setSelection(null)
      setSelectingSourceFor(null)
      return
    }

    if (selectingSourceFor === 'new') {
      if (draft.overlays.length >= 6) {
        setSelectingSourceFor(null)
        setSelection(null)
        return
      }
      const id = `region-${Date.now()}-${draft.overlays.length}`
      const region: LayoutRegion = {
        id,
        label: `Region ${draft.overlays.length + 1}`,
        source: finalSelection,
        destination: defaultDestination(
          finalSelection,
          sourceAspect,
          outputAspect,
          draft.overlays.length,
        ),
      }
      update({ ...draft, overlays: [...draft.overlays, region] })
      setSelectedId(id)
    } else {
      updateRegion(selectingSourceFor, (region) => ({
        ...region,
        source: finalSelection,
      }))
      setSelectedId(selectingSourceFor)
    }

    setSelectingSourceFor(null)
    setSelection(null)
  }

  const beginBaseDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!sourceStage.current || !draft || selectingSourceFor) return
    event.preventDefault()
    event.stopPropagation()
    const point = normalizedPoint(event, sourceStage.current)
    event.currentTarget.setPointerCapture(event.pointerId)
    sourceDrag.current = {
      kind: 'base',
      pointerId: event.pointerId,
      start: point,
      initial: baseCropRect(sourceAspect, outputAspect, draft),
      captureElement: event.currentTarget,
    }
  }

  const beginOutputDrag = (
    event: React.PointerEvent<HTMLElement>,
    region: LayoutRegion,
    kind: 'move' | 'resize',
  ) => {
    if (!outputStage.current) return
    event.preventDefault()
    event.stopPropagation()
    setSelectedId(region.id)
    event.currentTarget.setPointerCapture(event.pointerId)
    outputDrag.current = {
      pointerId: event.pointerId,
      id: region.id,
      kind,
      start: normalizedPoint(event, outputStage.current),
      initial: { ...region.destination },
      captureElement: event.currentTarget,
    }
  }

  const moveOutputPointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = outputDrag.current
    if (!drag || !outputStage.current) return
    const point = normalizedPoint(event, outputStage.current)
    const dx = point.x - drag.start.x
    const dy = point.y - drag.start.y

    updateRegion(drag.id, (region) => {
      const initial = drag.initial
      if (drag.kind === 'move') {
        return {
          ...region,
          destination: {
            ...initial,
            x: clamp(initial.x + dx, 0, 1 - initial.width),
            y: clamp(initial.y + dy, 0, 1 - initial.height),
          },
        }
      }

      return {
        ...region,
        destination: {
          ...initial,
          width: clamp(initial.width + dx, MIN_DESTINATION_SIZE, 1 - initial.x),
          height: clamp(initial.height + dy, MIN_DESTINATION_SIZE, 1 - initial.y),
        },
      }
    })
  }

  const endOutputPointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = outputDrag.current
    if (!drag) return
    outputDrag.current = null
    if (drag.captureElement.hasPointerCapture(event.pointerId)) {
      drag.captureElement.releasePointerCapture(event.pointerId)
    }
  }

  if (!supported) {
    return (
      <div className="border border-ink-800 bg-ink-900/80 p-4">
        <p className="eyebrow">Custom layout</p>
        <p className="mt-3 text-xs leading-relaxed text-ink-500">
          Custom crop layouts are available for 9:16 and 1:1 clips.
        </p>
      </div>
    )
  }

  if (!dimensionsReady) {
    return (
      <div className="border border-ink-800 bg-ink-900/80 p-4">
        <p className="eyebrow">Custom layout</p>
        <p className="mt-3 text-xs text-ink-500">
          Source dimensions are unavailable, so the visual layout editor cannot open.
        </p>
      </div>
    )
  }

  if (!draft) {
    return (
      <div className="border border-ink-800 bg-ink-900/80 p-4">
        <p className="eyebrow">Custom layout</p>
        <p className="mt-3 text-xs leading-relaxed text-ink-500">
          Use a fixed crop and visually pull extra regions from the original source into the
          final frame — for example gameplay, a VTuber, and Twitch chat.
        </p>
        <button type="button" onClick={enable} className="btn btn-primary mt-4">
          Start custom layout
        </button>
      </div>
    )
  }

  const selected = draft.overlays.find((region) => region.id === selectedId) ?? null
  const baseRect = baseCropRect(sourceAspect, outputAspect, draft)

  return (
    <div className="border border-ink-800 bg-ink-900/85 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3 border-b border-ink-800 pb-3">
        <div>
          <p className="eyebrow">Custom layout</p>
          <p className="mt-1 text-xs text-ink-500">
            Drag on the source to select regions. Drag them on the output to place them.
          </p>
        </div>
        <span className="numeric text-xs text-ink-600">{draft.overlays.length}/6 regions</span>
      </div>

      <div className="mt-4 grid gap-5 2xl:grid-cols-[minmax(0,1.35fr)_minmax(14rem,0.65fr)]">
        <div>
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-wide text-ink-400">
              Original source
            </p>
            <button
              type="button"
              onClick={() => beginSourceSelection('new')}
              disabled={draft.overlays.length >= 6}
              className={`btn ${
                selectingSourceFor === 'new' ? 'btn-primary' : 'btn-ghost'
              }`}
            >
              {selectingSourceFor === 'new' ? 'Drag a rectangle…' : '+ Select source region'}
            </button>
          </div>

          <div
            ref={sourceStage}
            className={[
              'relative w-full touch-none select-none overflow-hidden border bg-black',
              selectingSourceFor ? 'cursor-crosshair border-sodium-500' : 'border-ink-700',
            ].join(' ')}
            style={{ aspectRatio: String(sourceAspect) }}
            onDragStart={(event) => event.preventDefault()}
            onPointerDown={startSourcePointer}
            onPointerMove={moveSourcePointer}
            onPointerUp={endSourcePointer}
            onPointerCancel={endSourcePointer}
          >
            <SyncedVideo
              src={src}
              time={currentTime}
              className="pointer-events-none absolute inset-0 size-full select-none"
            />

            <div className="pointer-events-none absolute inset-0 bg-black/10" />

            <div
              className={[
                'absolute select-none border-2 border-dashed border-ink-100/80 bg-transparent',
                selectingSourceFor ? 'pointer-events-none' : 'cursor-move',
              ].join(' ')}
              style={rectStyle(baseRect)}
              onDragStart={(event) => event.preventDefault()}
              onPointerDown={beginBaseDrag}
              title="Drag to reposition the base crop"
            >
              <span className="absolute left-1 top-1 bg-black/65 px-1 text-[10px] text-ink-100">
                BASE
              </span>
            </div>

            {draft.overlays.map((region, index) => {
              // When reselecting a region, hide its old source rectangle so it
              // does not obscure the pixels the user is trying to crop again.
              if (selectingSourceFor === region.id) return null

              return (
                <button
                  key={region.id}
                  type="button"
                  draggable={false}
                  onDragStart={(event) => event.preventDefault()}
                  onPointerDown={(event) => {
                    if (selectingSourceFor) return
                    event.preventDefault()
                    event.stopPropagation()
                    setSelectedId(region.id)
                  }}
                  className={[
                    'absolute select-none border-2 text-left',
                    selectingSourceFor ? 'pointer-events-none' : '',
                    selectedId === region.id
                      ? 'border-sodium-400 bg-sodium-700/20'
                      : 'border-sodium-600/80 bg-sodium-700/10',
                  ].join(' ')}
                  style={rectStyle(region.source)}
                  title="Selected source region"
                >
                  <span className="absolute left-1 top-1 bg-black/65 px-1 text-[10px] text-sodium-300">
                    {region.label || `Region ${index + 1}`}
                  </span>
                </button>
              )
            })}

            {selection && (
              <div
                className="pointer-events-none absolute border-2 border-signal-good bg-signal-good/10"
                style={rectStyle(selection)}
              />
            )}
          </div>

          <p className="mt-2 text-xs leading-relaxed text-ink-600">
            Drag the dashed BASE frame to choose the 9:16/1:1 center. Click
            <span className="text-ink-400"> Select source region</span>, then drag directly over
            the VTuber, gameplay, chat, or anything else you want to pull out.
          </p>
        </div>

        <div>
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-400">
            Output placement
          </p>
          <div
            ref={outputStage}
            className="relative mx-auto w-full touch-none select-none overflow-hidden border border-ink-700 bg-black"
            style={{ aspectRatio: String(outputAspect) }}
            onDragStart={(event) => event.preventDefault()}
            onPointerMove={moveOutputPointer}
            onPointerUp={endOutputPointer}
            onPointerCancel={endOutputPointer}
          >
            <CroppedVideo src={src} time={currentTime} source={baseRect} />

            {draft.overlays.map((region, index) => (
              <div
                key={region.id}
                className="absolute overflow-hidden"
                style={rectStyle(region.destination)}
              >
                <CroppedVideo src={src} time={currentTime} source={region.source} />

                <div
                  draggable={false}
                  onDragStart={(event) => event.preventDefault()}
                  onPointerDown={(event) => beginOutputDrag(event, region, 'move')}
                  className={[
                    'absolute inset-0 select-none cursor-move border-2',
                    selectedId === region.id
                      ? 'border-sodium-400'
                      : 'border-transparent hover:border-sodium-600/70',
                  ].join(' ')}
                >
                  <span className="pointer-events-none absolute left-1 top-1 bg-black/65 px-1 text-[10px] text-sodium-300">
                    {region.label || `Region ${index + 1}`}
                  </span>
                  <button
                    type="button"
                    draggable={false}
                    aria-label="Resize overlay"
                    title="Drag to stretch"
                    onDragStart={(event) => event.preventDefault()}
                    onPointerDown={(event) => beginOutputDrag(event, region, 'resize')}
                    className="absolute -bottom-1.5 -right-1.5 size-5 cursor-nwse-resize touch-none border border-ink-900 bg-sodium-400"
                  />
                </div>
              </div>
            ))}
          </div>
          <p className="mt-2 text-xs leading-relaxed text-ink-600">
            Drag a region to move it. Drag its bottom-right square to stretch that fixed
            source crop wider or taller; resizing never changes the source selection.
          </p>
        </div>
      </div>

      {selected && (
        <div className="mt-4 flex flex-wrap items-end gap-3 border-t border-ink-800 pt-4">
          <label className="min-w-44 flex-1">
            <span className="eyebrow">Selected region</span>
            <input
              value={selected.label}
              onChange={(event) =>
                updateRegion(selected.id, (region) => ({ ...region, label: event.target.value }))
              }
              className="field mt-1 py-1 text-sm"
            />
          </label>
          <button
            type="button"
            onClick={() => beginSourceSelection(selected.id)}
            className={`btn ${
              selectingSourceFor === selected.id ? 'btn-primary' : 'btn-ghost'
            }`}
          >
            {selectingSourceFor === selected.id ? 'Drag replacement…' : 'Reselect source'}
          </button>
          <button
            type="button"
            onClick={() => removeRegion(selected.id)}
            className="btn btn-quiet text-signal-bad"
          >
            Remove
          </button>
        </div>
      )}

      <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-ink-800 pt-4">
        <button
          type="button"
          onClick={() => onSave(draft)}
          disabled={!dirty || saving}
          className="btn btn-primary"
        >
          {saving ? 'Saving…' : 'Save layout'}
        </button>
        <button
          type="button"
          onClick={() => {
            update(null)
            onSave(null)
          }}
          disabled={saving}
          className="btn btn-quiet"
        >
          Use AutoClip framing
        </button>
        {dirty && <span className="text-xs text-sodium-500">Unsaved layout changes</span>}
      </div>
    </div>
  )
}

type SourceDrag =
  | {
      kind: 'select'
      pointerId: number
      start: Point
      captureElement: HTMLElement
    }
  | {
      kind: 'base'
      pointerId: number
      start: Point
      initial: LayoutRect
      captureElement: HTMLElement
    }

type OutputDrag = {
  pointerId: number
  id: string
  kind: 'move' | 'resize'
  start: Point
  initial: LayoutRect
  captureElement: HTMLElement
}

type Point = { x: number; y: number }

function SyncedVideo({
  src,
  time,
  className,
  style,
}: {
  src: string
  time: number
  className?: string
  style?: React.CSSProperties
}) {
  const video = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    const element = video.current
    if (!element) return
    if (Math.abs(element.currentTime - time) > 0.04) element.currentTime = time
  }, [time])

  return (
    <video
      ref={video}
      src={src}
      muted
      playsInline
      preload="auto"
      draggable={false}
      onDragStart={(event) => event.preventDefault()}
      className={className}
      style={{ ...style, userSelect: 'none' }}
    />
  )
}

function CroppedVideo({
  src,
  time,
  source,
}: {
  src: string
  time: number
  source: LayoutRect
}) {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      <SyncedVideo
        src={src}
        time={time}
        className="pointer-events-none absolute max-w-none select-none"
        style={{
          width: `${100 / source.width}%`,
          height: `${100 / source.height}%`,
          maxWidth: 'none',
          maxHeight: 'none',
          objectFit: 'fill',
          left: `${(-source.x / source.width) * 100}%`,
          top: `${(-source.y / source.height) * 100}%`,
        }}
      />
    </div>
  )
}

function normalizedPoint(
  event: React.PointerEvent<HTMLElement>,
  element: HTMLElement,
): Point {
  const rect = element.getBoundingClientRect()
  return {
    x: clamp((event.clientX - rect.left) / rect.width, 0, 1),
    y: clamp((event.clientY - rect.top) / rect.height, 0, 1),
  }
}

function rectFromPoints(a: Point, b: Point): LayoutRect {
  const x = Math.min(a.x, b.x)
  const y = Math.min(a.y, b.y)
  return {
    x,
    y,
    width: Math.abs(a.x - b.x),
    height: Math.abs(a.y - b.y),
  }
}

function baseCropRect(
  sourceAspect: number,
  outputAspect: number,
  layout: ManualLayout,
): LayoutRect {
  let width = 1
  let height = 1

  if (sourceAspect >= outputAspect) {
    width = outputAspect / sourceAspect
  } else {
    height = sourceAspect / outputAspect
  }

  return {
    x: (1 - width) * layout.base_center_x,
    y: (1 - height) * layout.base_center_y,
    width,
    height,
  }
}

function defaultDestination(
  source: LayoutRect,
  sourceAspect: number,
  outputAspect: number,
  index: number,
): LayoutRect {
  const selectedPixelAspect =
    (source.width * sourceAspect) / Math.max(0.001, source.height)
  const normalizedAspect = selectedPixelAspect / outputAspect

  let height = 0.27
  let width = height * normalizedAspect
  if (width > 0.92) {
    width = 0.92
    height = width / Math.max(0.001, normalizedAspect)
  }

  width = clamp(width, MIN_DESTINATION_SIZE, 0.92)
  height = clamp(height, MIN_DESTINATION_SIZE, 0.32)
  const ySlots = [0.04, 0.36, 0.68]

  return {
    x: (1 - width) / 2,
    y: clamp(ySlots[index % ySlots.length], 0, 1 - height),
    width,
    height,
  }
}

function rectStyle(rect: LayoutRect): React.CSSProperties {
  return {
    left: `${rect.x * 100}%`,
    top: `${rect.y * 100}%`,
    width: `${rect.width * 100}%`,
    height: `${rect.height * 100}%`,
  }
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}
