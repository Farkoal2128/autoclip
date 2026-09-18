import { useEffect, useRef, useState } from 'react'

import { formatTimecode, type LayoutFrame, type LayoutPreset, type LayoutRect, type LayoutRegion, type ManualLayout } from '../api'

const EMPTY_LAYOUT: ManualLayout = {
  base_center_x: 0.5,
  base_center_y: 0.5,
  overlays: [],
  cues: [],
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
  presets = [],
  presetBusy = false,
  onPresetSave,
  onPresetApply,
  onPresetDelete,
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
  presets?: LayoutPreset[]
  presetBusy?: boolean
  onPresetSave?: (
    name: string,
    ratio: LayoutPreset['ratio'],
    layout: ManualLayout,
  ) => void
  onPresetApply?: (preset: LayoutPreset) => void
  onPresetDelete?: (preset: LayoutPreset) => void
  onPreviewChange: (layout: ManualLayout | null) => void
}) {
  const [draft, setDraft] = useState<ManualLayout | null>(layout)
  const [dirty, setDirty] = useState(false)
  const [selectedCueId, setSelectedCueId] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(
    layout?.overlays[0]?.id ?? null,
  )
  const [selectingSourceFor, setSelectingSourceFor] = useState<'new' | string | null>(null)
  const [selection, setSelection] = useState<LayoutRect | null>(null)
  const [presetName, setPresetName] = useState('')

  const sourceStage = useRef<HTMLDivElement>(null)
  const outputStage = useRef<HTMLDivElement>(null)
  const sourceDrag = useRef<SourceDrag | null>(null)
  const outputDrag = useRef<OutputDrag | null>(null)

  const supported = ratio === '9:16' || ratio === '1:1'
  const dimensionsReady = Boolean(sourceWidth && sourceHeight)
  const sourceAspect =
    sourceWidth && sourceHeight && sourceHeight > 0 ? sourceWidth / sourceHeight : 16 / 9
  const outputAspect = ratio === '1:1' ? 1 : 9 / 16
  const presetRatio: LayoutPreset['ratio'] = ratio === '1:1' ? '1:1' : '9:16'
  const selectedCue = draft?.cues.find((cue) => cue.id === selectedCueId) ?? null
  const workingFrame: LayoutFrame | null = selectedCue?.layout ?? draft

  useEffect(() => {
    setDraft(layout)
    setDirty(false)
    setSelectedCueId(null)
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
    const next = { ...EMPTY_LAYOUT, overlays: [], cues: [] }
    update(next)
    setSelectedCueId(null)
    setSelectedId(null)
  }

  const updateWorkingFrame = (updater: (frame: LayoutFrame) => LayoutFrame) => {
    if (!draft || !workingFrame) return
    const nextFrame = updater(workingFrame)
    if (selectedCueId) {
      update({
        ...draft,
        cues: draft.cues.map((cue) =>
          cue.id === selectedCueId ? { ...cue, layout: nextFrame } : cue,
        ),
      })
      return
    }
    update({
      ...draft,
      base_center_x: nextFrame.base_center_x,
      base_center_y: nextFrame.base_center_y,
      overlays: nextFrame.overlays,
    })
  }

  const selectTimelineFrame = (cueId: string | null) => {
    setSelectedCueId(cueId)
    const frame =
      cueId && draft
        ? draft.cues.find((cue) => cue.id === cueId)?.layout ?? draft
        : draft
    setSelectedId(frame?.overlays[0]?.id ?? null)
    setSelectingSourceFor(null)
    setSelection(null)
  }

  const addCueAtPlayhead = () => {
    if (!draft) return
    const sourceFrame = layoutFrameAtTime(draft, currentTime)
    const cue = {
      id: `layout-${Date.now()}`,
      at_s: currentTime,
      transition: 'cut' as const,
      lead_s: 0,
      layout: cloneFrame(sourceFrame),
    }
    const next = {
      ...draft,
      cues: [...draft.cues.filter((item) => Math.abs(item.at_s - currentTime) > 0.005), cue].sort(
        (a, b) => a.at_s - b.at_s,
      ),
    }
    update(next)
    setSelectedCueId(cue.id)
    setSelectedId(cue.layout.overlays[0]?.id ?? null)
  }

  const updateSelectedCue = (
    patch: Partial<{ at_s: number; transition: 'cut' | 'glide'; lead_s: number }>,
  ) => {
    if (!draft || !selectedCueId) return
    update({
      ...draft,
      cues: draft.cues
        .map((cue) => (cue.id === selectedCueId ? { ...cue, ...patch } : cue))
        .sort((a, b) => a.at_s - b.at_s),
    })
  }

  const removeSelectedCue = () => {
    if (!draft || !selectedCueId) return
    update({ ...draft, cues: draft.cues.filter((cue) => cue.id !== selectedCueId) })
    setSelectedCueId(null)
    setSelectedId(workingFrame!.overlays[0]?.id ?? null)
  }

  const applyPresetToWorkingFrame = (preset: LayoutPreset) => {
    if (!draft) {
      onPresetApply?.(preset)
      return
    }
    updateWorkingFrame(() => ({
      base_center_x: preset.layout.base_center_x,
      base_center_y: preset.layout.base_center_y,
      overlays: preset.layout.overlays.map(cloneRegion),
    }))
  }

  const updateRegion = (id: string, updater: (region: LayoutRegion) => LayoutRegion) => {
    if (!workingFrame) return
    updateWorkingFrame((frame) => ({
      ...frame,
      overlays: frame.overlays.map((region) => (region.id === id ? updater(region) : region)),
    }))
  }

  const removeRegion = (id: string) => {
    if (!workingFrame) return
    const overlays = workingFrame.overlays.filter((region) => region.id !== id)
    updateWorkingFrame((frame) => ({ ...frame, overlays }))
    setSelectedId((current) => (current === id ? overlays[0]?.id ?? null : current))
  }

  const beginSourceSelection = (target: 'new' | string) => {
    setSelectingSourceFor(target)
    setSelection(null)
  }

  const startSourcePointer = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!sourceStage.current || !workingFrame) return
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
    const crop = baseCropRect(sourceAspect, outputAspect, workingFrame!)
    const centerX = clamp(point.x - crop.width / 2, 0, 1 - crop.width)
    const centerY = clamp(point.y - crop.height / 2, 0, 1 - crop.height)
    updateWorkingFrame((frame) => ({
      ...frame,
      base_center_x: crop.width >= 0.999 ? 0.5 : centerX / (1 - crop.width),
      base_center_y: crop.height >= 0.999 ? 0.5 : centerY / (1 - crop.height),
    }))
  }

  const moveSourcePointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = sourceDrag.current
    if (!drag || !sourceStage.current || !workingFrame) return
    const point = normalizedPoint(event, sourceStage.current)

    if (drag.kind === 'select') {
      setSelection(rectFromPoints(drag.start, point))
      return
    }

    const dx = point.x - drag.start.x
    const dy = point.y - drag.start.y
    const x = clamp(drag.initial.x + dx, 0, 1 - drag.initial.width)
    const y = clamp(drag.initial.y + dy, 0, 1 - drag.initial.height)
    updateWorkingFrame((frame) => ({
      ...frame,
      base_center_x:
        drag.initial.width >= 0.999 ? 0.5 : x / Math.max(0.001, 1 - drag.initial.width),
      base_center_y:
        drag.initial.height >= 0.999 ? 0.5 : y / Math.max(0.001, 1 - drag.initial.height),
    }))
  }

  const endSourcePointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = sourceDrag.current
    const stage = sourceStage.current
    if (!drag) return
    sourceDrag.current = null

    if (drag.captureElement.hasPointerCapture(event.pointerId)) {
      drag.captureElement.releasePointerCapture(event.pointerId)
    }

    if (drag.kind !== 'select' || !workingFrame || !selectingSourceFor || !stage) {
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
      if (workingFrame!.overlays.length >= 6) {
        setSelectingSourceFor(null)
        setSelection(null)
        return
      }
      const id = `region-${Date.now()}-${workingFrame!.overlays.length}`
      const region: LayoutRegion = {
        id,
        label: `Region ${workingFrame!.overlays.length + 1}`,
        source: finalSelection,
        destination: defaultDestination(
          finalSelection,
          sourceAspect,
          outputAspect,
          workingFrame!.overlays.length,
        ),
      }
      updateWorkingFrame((frame) => ({ ...frame, overlays: [...frame.overlays, region] }))
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
    if (!sourceStage.current || !workingFrame || selectingSourceFor) return
    event.preventDefault()
    event.stopPropagation()
    const point = normalizedPoint(event, sourceStage.current)
    event.currentTarget.setPointerCapture(event.pointerId)
    sourceDrag.current = {
      kind: 'base',
      pointerId: event.pointerId,
      start: point,
      initial: baseCropRect(sourceAspect, outputAspect, workingFrame!),
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

      if (event.altKey) {
        return {
          ...region,
          destination: centeredAspectResize(initial, drag.start, point),
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
        <PresetPanel
          presets={presets}
          busy={presetBusy}
          onApply={onPresetApply}
          onDelete={onPresetDelete}
        />
        <button type="button" onClick={enable} className="btn btn-primary mt-4">
          Start custom layout
        </button>
      </div>
    )
  }

  const selected = workingFrame!.overlays.find((region) => region.id === selectedId) ?? null
  const baseRect = baseCropRect(sourceAspect, outputAspect, workingFrame!)

  return (
    <div className="border border-ink-800 bg-ink-900/85 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3 border-b border-ink-800 pb-3">
        <div>
          <p className="eyebrow">Custom layout</p>
          <p className="mt-1 text-xs text-ink-500">
            Drag on the source to select regions. Drag them on the output to place them.
          </p>
        </div>
        <span className="numeric text-xs text-ink-600">
          {workingFrame!.overlays.length}/6 regions · {draft.cues.length + 1} layout points
        </span>
      </div>

      <PresetPanel
        presets={presets}
        busy={presetBusy}
        draft={frameAsManualLayout(workingFrame!)}
        ratio={presetRatio}
        name={presetName}
        onNameChange={setPresetName}
        onSave={(name, presetRatioValue, layoutValue) => {
          onPresetSave?.(name, presetRatioValue, layoutValue)
          setPresetName('')
        }}
        onApply={applyPresetToWorkingFrame}
        onDelete={onPresetDelete}
      />

      <div className="mt-4 grid gap-3 border-b border-ink-800 pb-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="eyebrow">Layout timeline</p>
            <p className="mt-1 text-xs text-ink-500">
              Add a layout at the playhead, then edit that point independently.
            </p>
          </div>
          <button type="button" onClick={addCueAtPlayhead} className="btn btn-primary">
            + Layout at {formatTimecode(currentTime)}
          </button>
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => selectTimelineFrame(null)}
            className={`btn ${selectedCueId === null ? 'btn-primary' : 'btn-ghost'}`}
          >
            Start
          </button>
          {draft.cues.map((cue) => (
            <button
              key={cue.id}
              type="button"
              onClick={() => selectTimelineFrame(cue.id)}
              className={`btn ${selectedCueId === cue.id ? 'btn-primary' : 'btn-ghost'}`}
            >
              {formatTimecode(cue.at_s)}
            </button>
          ))}
        </div>

        {selectedCue && (
          <div className="grid gap-3 md:grid-cols-[auto_auto_minmax(10rem,1fr)_auto] md:items-end">
            <button
              type="button"
              onClick={() => updateSelectedCue({ at_s: currentTime })}
              className="btn btn-ghost"
              title="Move this layout change to the current playhead position"
            >
              Set time to {formatTimecode(currentTime)}
            </button>
            <label>
              <span className="eyebrow">Base movement</span>
              <select
                value={selectedCue.transition}
                onChange={(event) =>
                  updateSelectedCue({ transition: event.target.value as 'cut' | 'glide' })
                }
                className="field mt-1"
              >
                <option value="cut">Cut at timestamp</option>
                <option value="glide">Glide into position</option>
              </select>
            </label>
            <label>
              <span className="eyebrow">Start moving before cut</span>
              <div className="mt-1 flex items-center gap-2">
                <input
                  type="number"
                  min={0}
                  max={30}
                  step={0.1}
                  value={selectedCue.lead_s}
                  disabled={selectedCue.transition !== 'glide'}
                  onChange={(event) =>
                    updateSelectedCue({
                      lead_s: Math.max(0, Math.min(30, Number(event.target.value) || 0)),
                    })
                  }
                  className="field min-w-0 flex-1"
                />
                <span className="text-xs text-ink-500">seconds</span>
              </div>
              <span className="mt-1 block text-[11px] leading-relaxed text-ink-600">
                The base arrives at exactly {formatTimecode(selectedCue.at_s)}.
              </span>
            </label>
            <button
              type="button"
              onClick={removeSelectedCue}
              className="btn btn-quiet text-signal-bad"
            >
              Remove point
            </button>
          </div>
        )}
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
              disabled={workingFrame!.overlays.length >= 6}
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

            {workingFrame!.overlays.map((region, index) => {
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

            {workingFrame!.overlays.map((region, index) => (
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
                    title="Drag to resize · hold Alt to scale from center without stretching"
                    onDragStart={(event) => event.preventDefault()}
                    onPointerDown={(event) => beginOutputDrag(event, region, 'resize')}
                    className="absolute -bottom-1.5 -right-1.5 size-5 cursor-nwse-resize touch-none border border-ink-900 bg-sodium-400"
                  />
                </div>
              </div>
            ))}
          </div>
          <p className="mt-2 text-xs leading-relaxed text-ink-600">
            Drag a region to move it. Drag its bottom-right square to resize it. Hold
            <span className="text-ink-400"> Alt</span> while resizing to scale from the center
            and preserve the region&apos;s aspect ratio, preventing stretch distortion.
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

function PresetPanel({
  presets,
  busy,
  draft = null,
  ratio = '9:16',
  name = '',
  onNameChange,
  onSave,
  onApply,
  onDelete,
}: {
  presets: LayoutPreset[]
  busy: boolean
  draft?: ManualLayout | null
  ratio?: LayoutPreset['ratio']
  name?: string
  onNameChange?: (name: string) => void
  onSave?: (name: string, ratio: LayoutPreset['ratio'], layout: ManualLayout) => void
  onApply?: (preset: LayoutPreset) => void
  onDelete?: (preset: LayoutPreset) => void
}) {
  return (
    <div className="mt-4 border-y border-ink-800 py-4">
      <div>
        <p className="eyebrow">Quick layout presets</p>
        <p className="mt-1 text-xs text-ink-500">
          Apply a saved layout or save this editor state for any future clip.
        </p>
      </div>

      {presets.length > 0 ? (
        <div className="mt-3 space-y-2">
          {presets.map((preset) => (
            <div key={preset.id} className="flex items-center gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => onApply?.(preset)}
                className="btn btn-ghost min-w-0 flex-1 justify-between"
              >
                <span className="truncate">{preset.name}</span>
                <span className="numeric ml-3 shrink-0 text-xs text-ink-500">
                  {preset.ratio}
                </span>
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => onDelete?.(preset)}
                className="btn btn-quiet text-signal-bad"
                aria-label={`Delete ${preset.name} preset`}
                title="Delete preset"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      ) : (
        <p className="mt-3 text-xs text-ink-600">No saved layouts yet.</p>
      )}

      {draft && onSave && onNameChange && (
        <div className="mt-4 flex gap-2">
          <input
            value={name}
            onChange={(event) => onNameChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && name.trim()) {
                onSave(name.trim(), ratio, draft)
              }
            }}
            placeholder="Preset name"
            className="field min-w-0 flex-1 text-sm"
            maxLength={80}
          />
          <button
            type="button"
            disabled={busy || !name.trim()}
            onClick={() => onSave(name.trim(), ratio, draft)}
            className="btn btn-primary"
          >
            Save preset
          </button>
        </div>
      )}
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

function cloneRegion(region: LayoutRegion): LayoutRegion {
  return {
    ...region,
    source: { ...region.source },
    destination: { ...region.destination },
  }
}

function cloneFrame(frame: LayoutFrame): LayoutFrame {
  return {
    base_center_x: frame.base_center_x,
    base_center_y: frame.base_center_y,
    overlays: frame.overlays.map(cloneRegion),
  }
}

function frameAsManualLayout(frame: LayoutFrame): ManualLayout {
  return { ...cloneFrame(frame), cues: [] }
}

function layoutFrameAtTime(layout: ManualLayout, sourceTime: number): LayoutFrame {
  const cues = [...layout.cues].sort((a, b) => a.at_s - b.at_s)
  let frame: LayoutFrame = layout
  for (const cue of cues) {
    if (cue.at_s > sourceTime) break
    frame = cue.layout
  }
  return frame
}

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

function centeredAspectResize(
  initial: LayoutRect,
  start: Point,
  point: Point,
): LayoutRect {
  const centerX = initial.x + initial.width / 2
  const centerY = initial.y + initial.height / 2

  const startX = start.x - centerX
  const startY = start.y - centerY
  const currentX = point.x - centerX
  const currentY = point.y - centerY

  const startDistance = Math.hypot(startX, startY)
  const currentDistance = Math.hypot(currentX, currentY)

  const minScale = Math.max(
    MIN_DESTINATION_SIZE / initial.width,
    MIN_DESTINATION_SIZE / initial.height,
  )
  const maxScale = Math.min(
    (2 * Math.min(centerX, 1 - centerX)) / initial.width,
    (2 * Math.min(centerY, 1 - centerY)) / initial.height,
  )

  const sameDirection = startX * currentX + startY * currentY > 0
  const rawScale =
    sameDirection && startDistance > 0.000001 ? currentDistance / startDistance : minScale
  const scale = clamp(rawScale, minScale, Math.max(minScale, maxScale))

  const width = initial.width * scale
  const height = initial.height * scale

  return {
    x: centerX - width / 2,
    y: centerY - height / 2,
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
