import { useEffect, useRef, useState } from 'react'

import {
  api,
  formatTimecode,
  type LayoutFrame,
  type LayoutPreset,
  type LayoutRect,
  type LayoutRegion,
  type ManualLayout,
  type TwitchChatMessage,
  type TwitchChatOverlay,
} from '../api'

const EMPTY_LAYOUT: ManualLayout = {
  base_center_x: 0.5,
  base_center_y: 0.5,
  overlays: [],
  chat_overlays: [],
  cues: [],
}

const MIN_REGION_SIZE = 0.03
const MIN_DESTINATION_SIZE = 0.08

export function LayoutEditor({
  clipId,
  layout,
  ratio,
  src,
  currentTime,
  clipStartS,
  clipEndS,
  sourceWidth,
  sourceHeight,
  saving,
  onSave,
  presets = [],
  presetBusy = false,
  onPresetSave,
  onPresetApply,
  onPresetDelete,
  twitchChatAvailable = false,
  onLoadTwitchChat,
  onPreviewChange,
  onDirtyChange,
  onDraftChange,
}: {
  clipId?: string
  layout: ManualLayout | null
  ratio: string
  src: string
  currentTime: number
  clipStartS: number
  clipEndS: number
  sourceWidth: number | null
  sourceHeight: number | null
  saving: boolean
  onSave: (
    layout: ManualLayout | null,
  ) => boolean | void | Promise<boolean | void>
  presets?: LayoutPreset[]
  presetBusy?: boolean
  onPresetSave?: (
    name: string,
    ratio: LayoutPreset['ratio'],
    layout: ManualLayout,
  ) => void
  onPresetApply?: (preset: LayoutPreset) => void
  onPresetDelete?: (preset: LayoutPreset) => void
  twitchChatAvailable?: boolean
  onLoadTwitchChat?: () => Promise<TwitchChatMessage[]>
  onPreviewChange: (layout: ManualLayout | null) => void
  onDirtyChange?: (dirty: boolean) => void
  onDraftChange?: (layout: ManualLayout | null) => void
}) {
  const [draft, setDraft] = useState<ManualLayout | null>(layout)
  const [dirty, setDirty] = useState(false)
  const [previewMode, setPreviewMode] = useState<'custom' | 'auto'>(
    layout ? 'custom' : 'auto',
  )
  const [selectedCueId, setSelectedCueId] = useState<string | null>(() =>
    activeCueIdAtTime(layout, currentTime),
  )
  const [selectedId, setSelectedId] = useState<string | null>(() =>
    layout ? layoutFrameAtTime(layout, currentTime).overlays[0]?.id ?? null : null,
  )
  const [selectedChatId, setSelectedChatId] = useState<string | null>(null)
  const [studioTab, setStudioTab] = useState<'layout' | 'chat'>('layout')
  const [chatMessages, setChatMessages] = useState<TwitchChatMessage[] | null>(null)
  const [chatLoading, setChatLoading] = useState(false)
  const [chatError, setChatError] = useState<string | null>(null)
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
    const activeCueId = activeCueIdAtTime(layout, currentTime)
    const activeFrame = layout ? layoutFrameAtTime(layout, currentTime) : null

    setDraft(layout)
    setDirty(false)
    setPreviewMode(layout ? 'custom' : 'auto')
    onDraftChange?.(layout)
    setSelectedCueId(activeCueId)
    setSelectedId(activeFrame?.overlays[0]?.id ?? null)
    setSelectedChatId(null)
    setSelectingSourceFor(null)
    setSelection(null)
    onPreviewChange(layout)
  }, [layout, onDraftChange, onPreviewChange])

  useEffect(() => {
    onDirtyChange?.(dirty)
  }, [dirty, onDirtyChange])

  useEffect(() => {
    setStudioTab('layout')
    setChatMessages(null)
    setChatError(null)
    setChatLoading(false)
    setSelectedChatId(null)
  }, [clipId])

  const update = (next: ManualLayout | null) => {
    setDraft(next)
    setDirty(true)
    setPreviewMode(next ? 'custom' : 'auto')
    onDraftChange?.(next)
    onPreviewChange(next)
  }

  const showAutoFraming = () => {
    setPreviewMode('auto')
    onPreviewChange(null)
  }

  const showCustomLayout = () => {
    if (!draft) return
    setPreviewMode('custom')
    onPreviewChange(draft)
  }

  const saveDraft = async () => {
    if (!draft) return
    setPreviewMode('custom')
    onPreviewChange(draft)
    const saved = await onSave(draft)
    if (saved !== false) setDirty(false)
  }

  const enable = () => {
    const next = { ...EMPTY_LAYOUT, overlays: [], chat_overlays: [], cues: [] }
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
      chat_overlays: nextFrame.chat_overlays,
    })
  }

  const selectTimelineFrame = (cueId: string | null) => {
    if (draft) {
      setPreviewMode('custom')
      onPreviewChange(draft)
    }
    setSelectedCueId(cueId)
    const frame =
      cueId && draft
        ? draft.cues.find((cue) => cue.id === cueId)?.layout ?? draft
        : draft
    setSelectedId(frame?.overlays[0]?.id ?? null)
    setSelectedChatId(null)
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
      lead_s: 1,
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
    setSelectedChatId(null)
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
    setSelectedChatId(null)
    setSelectedId(draft.overlays[0]?.id ?? null)
  }

  const applyPresetToWorkingFrame = (preset: LayoutPreset) => {
    if (!draft) {
      onPresetApply?.(preset)
      return
    }
    updateWorkingFrame((frame) => ({
      base_center_x: preset.layout.base_center_x,
      base_center_y: preset.layout.base_center_y,
      overlays: preset.layout.overlays.map(cloneRegion),
      // A saved preset should not import chat from another VOD.
      chat_overlays: frame.chat_overlays,
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

  const updateChatOverlay = (
    id: string,
    updater: (overlay: TwitchChatOverlay) => TwitchChatOverlay,
  ) => {
    if (!workingFrame) return
    updateWorkingFrame((frame) => ({
      ...frame,
      chat_overlays: frame.chat_overlays.map((overlay) =>
        overlay.id === id ? updater(overlay) : overlay,
      ),
    }))
  }

  const removeChatOverlay = (id: string) => {
    if (!workingFrame) return
    const chatOverlays = workingFrame.chat_overlays.filter((overlay) => overlay.id !== id)
    updateWorkingFrame((frame) => ({ ...frame, chat_overlays: chatOverlays }))
    setSelectedChatId((current) => (current === id ? null : current))
  }

  const loadTwitchChat = async () => {
    if (!onLoadTwitchChat || chatLoading) return
    setChatLoading(true)
    setChatError(null)
    try {
      setChatMessages(await onLoadTwitchChat())
    } catch (error) {
      setChatError(error instanceof Error ? error.message : String(error))
    } finally {
      setChatLoading(false)
    }
  }

  const addChatMessage = (message: TwitchChatMessage) => {
    if (!workingFrame) return
    const existing = workingFrame.chat_overlays.find(
      (overlay) => overlay.message_id === message.id,
    )
    if (existing) {
      setSelectedId(null)
      setSelectedChatId(existing.id)
      return
    }
    if (workingFrame.chat_overlays.length >= 8) return

    const id = `chat-${Date.now()}-${workingFrame.chat_overlays.length}`
    const latestStart = Math.max(clipStartS, clipEndS - 0.1)
    const visibleFrom = clamp(message.offset_s, clipStartS, latestStart)
    const visibleUntil = clamp(
      visibleFrom + 5,
      Math.min(clipEndS, visibleFrom + 0.1),
      clipEndS,
    )
    const overlay: TwitchChatOverlay = {
      ...message,
      id,
      message_id: message.id,
      visible_from_s: visibleFrom,
      visible_until_s: visibleUntil,
      destination: defaultChatDestination(
        workingFrame.chat_overlays.length,
        ratio,
        message,
      ),
    }
    updateWorkingFrame((frame) => ({
      ...frame,
      chat_overlays: [...frame.chat_overlays, overlay],
    }))
    setSelectedId(null)
    setSelectedChatId(id)
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
      setSelectedChatId(null)
      setSelectedId(id)
    } else {
      updateRegion(selectingSourceFor, (region) => ({
        ...region,
        source: finalSelection,
      }))
      setSelectedChatId(null)
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
    item: { id: string; destination: LayoutRect },
    kind: 'move' | 'resize',
    target: 'region' | 'chat' = 'region',
  ) => {
    if (!outputStage.current) return
    event.preventDefault()
    event.stopPropagation()
    if (target === 'chat') {
      setSelectedId(null)
      setSelectedChatId(item.id)
    } else {
      setSelectedChatId(null)
      setSelectedId(item.id)
    }
    event.currentTarget.setPointerCapture(event.pointerId)
    outputDrag.current = {
      pointerId: event.pointerId,
      id: item.id,
      target,
      kind,
      start: normalizedPoint(event, outputStage.current),
      initial: { ...item.destination },
      captureElement: event.currentTarget,
    }
  }

  const moveOutputPointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = outputDrag.current
    if (!drag || !outputStage.current) return
    const point = normalizedPoint(event, outputStage.current)
    const dx = point.x - drag.start.x
    const dy = point.y - drag.start.y
    const initial = drag.initial

    let destination: LayoutRect
    if (drag.kind === 'move') {
      destination = {
        ...initial,
        x: clamp(initial.x + dx, 0, 1 - initial.width),
        y: clamp(initial.y + dy, 0, 1 - initial.height),
      }
    } else if (drag.target === 'chat' || event.altKey) {
      destination = centeredAspectResize(initial, drag.start, point)
    } else {
      destination = {
        ...initial,
        width: clamp(initial.width + dx, MIN_DESTINATION_SIZE, 1 - initial.x),
        height: clamp(initial.height + dy, MIN_DESTINATION_SIZE, 1 - initial.y),
      }
    }

    if (drag.target === 'chat') {
      updateChatOverlay(drag.id, (overlay) => ({ ...overlay, destination }))
    } else {
      updateRegion(drag.id, (region) => ({ ...region, destination }))
    }
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
  const selectedChat =
    workingFrame!.chat_overlays.find((overlay) => overlay.id === selectedChatId) ?? null
  const selectedCueIndex = selectedCue
    ? draft.cues.findIndex((cue) => cue.id === selectedCue.id)
    : -1
  const baseRect = baseCropRect(sourceAspect, outputAspect, workingFrame!)

  return (
    <div className="contents">
      <aside className="min-h-0 min-w-0 border border-ink-800 bg-ink-900/90 xl:col-start-1 xl:row-start-1">
        <div className="flex h-full min-h-0 flex-col">
          <div className="shrink-0 border-b border-ink-800 px-4 py-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="eyebrow">Layout studio</p>
                <p className="mt-1 text-xs leading-relaxed text-ink-500">
                  Timeline, transitions, and saved compositions.
                </p>
              </div>
              <span className="numeric shrink-0 text-[11px] text-ink-600">
                {workingFrame!.overlays.length}/6 · {workingFrame!.chat_overlays.length} chat ·{' '}
                {draft.cues.length + 1} pts
              </span>
            </div>
          </div>

          {twitchChatAvailable && (
            <div className="grid shrink-0 grid-cols-2 border-b border-ink-800">
              <button
                type="button"
                onClick={() => setStudioTab('layout')}
                className={[
                  'px-4 py-2 text-xs font-semibold transition-colors',
                  studioTab === 'layout'
                    ? 'bg-ink-850 text-sodium-500'
                    : 'text-ink-500 hover:text-ink-200',
                ].join(' ')}
              >
                Layout
              </button>
              <button
                type="button"
                onClick={() => setStudioTab('chat')}
                className={[
                  'px-4 py-2 text-xs font-semibold transition-colors',
                  studioTab === 'chat'
                    ? 'bg-ink-850 text-sodium-500'
                    : 'text-ink-500 hover:text-ink-200',
                ].join(' ')}
              >
                Twitch chat
              </button>
            </div>
          )}

          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
            <div className={studioTab === 'layout' ? '' : 'hidden'}>
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

            <section className="mt-4">
              <p className="eyebrow">Layout timeline</p>
              <button
                type="button"
                onClick={addCueAtPlayhead}
                className="btn btn-primary mt-2 w-full justify-between"
              >
                <span>+ Add layout</span>
                <span className="numeric">{formatTimecode(currentTime)}</span>
              </button>

              <div className="mt-2 flex gap-2 overflow-x-auto pb-1">
                <button
                  type="button"
                  onClick={() => selectTimelineFrame(null)}
                  className={[
                    'min-w-[5.75rem] shrink-0 border px-3 py-2 text-left transition-colors',
                    selectedCueId === null
                      ? 'border-sodium-500 bg-sodium-500 text-ink-900'
                      : 'border-ink-700 bg-ink-850 text-ink-200 hover:border-ink-600',
                  ].join(' ')}
                >
                  <span className="block text-[10px] font-semibold uppercase tracking-[0.14em]">
                    Start
                  </span>
                  <span className="mt-0.5 block text-[11px]">Initial</span>
                </button>

                {draft.cues.map((cue, index) => {
                  const active = selectedCueId === cue.id
                  return (
                    <button
                      key={cue.id}
                      type="button"
                      onClick={() => selectTimelineFrame(cue.id)}
                      className={[
                        'min-w-[6.5rem] shrink-0 border px-3 py-2 text-left transition-colors',
                        active
                          ? 'border-sodium-500 bg-sodium-500 text-ink-900'
                          : 'border-ink-700 bg-ink-850 text-ink-200 hover:border-ink-600',
                      ].join(' ')}
                    >
                      <span className="block text-[10px] font-semibold uppercase tracking-[0.14em]">
                        Point {index + 1}
                      </span>
                      <span className="numeric mt-0.5 block text-[11px] font-semibold">
                        {formatTimecode(cue.at_s)}
                      </span>
                    </button>
                  )
                })}
              </div>
            </section>

            <section className="mt-4 border-t border-ink-800 pt-4">
              {selectedCue ? (
                <>
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <p className="eyebrow">Selected point</p>
                      <p className="numeric mt-1 text-lg font-semibold text-ink-100">
                        {formatTimecode(selectedCue.at_s)}
                      </p>
                      <p className="mt-0.5 text-[11px] text-ink-600">
                        Point {selectedCueIndex + 1} of {draft.cues.length}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={removeSelectedCue}
                      className="btn btn-quiet text-signal-bad"
                    >
                      Remove
                    </button>
                  </div>

                  <button
                    type="button"
                    onClick={() => updateSelectedCue({ at_s: currentTime })}
                    className="btn btn-ghost mt-3 w-full justify-between"
                    title="Move this layout point to the current playhead"
                  >
                    <span>Arrival → playhead</span>
                    <span className="numeric text-ink-400">
                      {formatTimecode(currentTime)}
                    </span>
                  </button>

                  <div className="mt-4">
                    <p className="eyebrow">Base movement</p>
                    <div className="mt-2 grid grid-cols-2 gap-2">
                      <button
                        type="button"
                        onClick={() => updateSelectedCue({ transition: 'cut' })}
                        className={
                          selectedCue.transition === 'cut' ? 'btn btn-primary' : 'btn btn-ghost'
                        }
                      >
                        Cut
                      </button>
                      <button
                        type="button"
                        onClick={() => updateSelectedCue({ transition: 'glide' })}
                        className={
                          selectedCue.transition === 'glide' ? 'btn btn-primary' : 'btn btn-ghost'
                        }
                      >
                        Glide
                      </button>
                    </div>
                  </div>

                  {selectedCue.transition === 'glide' && (
                    <label className="mt-4 block">
                      <span className="eyebrow">Start moving before arrival</span>
                      <div className="mt-1 flex items-end gap-2">
                        <input
                          type="number"
                          min={0}
                          max={30}
                          step={0.1}
                          value={selectedCue.lead_s}
                          onChange={(event) =>
                            updateSelectedCue({
                              lead_s: Math.max(
                                0,
                                Math.min(30, Number(event.target.value) || 0),
                              ),
                            })
                          }
                          className="field numeric min-w-0 flex-1 text-base"
                        />
                        <span className="pb-2.5 text-xs text-ink-500">sec</span>
                      </div>
                      <span className="mt-1 block text-[11px] leading-relaxed text-ink-600">
                        Arrives exactly at {formatTimecode(selectedCue.at_s)}.
                      </span>
                    </label>
                  )}
                </>
              ) : (
                <>
                  <p className="eyebrow">Start layout</p>
                  <p className="mt-1 text-sm font-semibold text-ink-100">Initial composition</p>
                  <p className="mt-2 text-[11px] leading-relaxed text-ink-600">
                    Used from the clip start until the first layout point.
                  </p>
                </>
              )}
            </section>

            {selected && (
              <section className="mt-4 border-t border-ink-800 pt-4">
                <div className="flex items-center justify-between gap-2">
                  <p className="eyebrow">Selected region</p>
                  <button
                    type="button"
                    onClick={() => removeRegion(selected.id)}
                    className="btn btn-quiet text-signal-bad"
                  >
                    Remove
                  </button>
                </div>
                <input
                  value={selected.label}
                  aria-label="Selected region label"
                  onChange={(event) =>
                    updateRegion(selected.id, (region) => ({
                      ...region,
                      label: event.target.value,
                    }))
                  }
                  className="field mt-1 min-w-0 py-1.5 text-sm"
                />
                <button
                  type="button"
                  onClick={() => beginSourceSelection(selected.id)}
                  className={`btn mt-2 w-full ${
                    selectingSourceFor === selected.id ? 'btn-primary' : 'btn-ghost'
                  }`}
                >
                  {selectingSourceFor === selected.id ? 'Drag replacement…' : 'Reselect source'}
                </button>
              </section>
            )}

            {selectedChat && (
              <section className="mt-4 border-t border-ink-800 pt-4">
                <div className="flex items-center justify-between gap-2">
                  <p className="eyebrow">Selected chat message</p>
                  <button
                    type="button"
                    onClick={() => removeChatOverlay(selectedChat.id)}
                    className="btn btn-quiet text-signal-bad"
                  >
                    Remove
                  </button>
                </div>
                <p className="mt-2 truncate text-xs font-semibold text-ink-200">
                  {selectedChat.username}
                </p>
                <p className="mt-1 line-clamp-3 text-xs leading-relaxed text-ink-500">
                  {selectedChat.message}
                </p>

                <div className="mt-3 border-t border-ink-800 pt-3">
                  <p className="eyebrow">Visibility</p>
                  <div className="mt-2 grid gap-3">
                    <label>
                      <span className="text-[11px] text-ink-500">Appears at</span>
                      <div className="mt-1 flex items-center gap-2">
                        <input
                          type="number"
                          min={clipStartS}
                          max={Math.max(clipStartS, clipEndS - 0.1)}
                          step={0.1}
                          value={selectedChat.visible_from_s ?? clipStartS}
                          onChange={(event) => {
                            const next = clamp(
                              Number(event.target.value) || clipStartS,
                              clipStartS,
                              Math.max(clipStartS, clipEndS - 0.1),
                            )
                            updateChatOverlay(selectedChat.id, (overlay) => ({
                              ...overlay,
                              visible_from_s: next,
                              visible_until_s: Math.max(
                                next + 0.1,
                                overlay.visible_until_s ?? clipEndS,
                              ),
                            }))
                          }}
                          className="field numeric min-w-0 flex-1 py-1.5"
                        />
                        <button
                          type="button"
                          className="btn btn-ghost px-2 py-1.5 text-[11px]"
                          onClick={() =>
                            updateChatOverlay(selectedChat.id, (overlay) => ({
                              ...overlay,
                              visible_from_s: clamp(
                                currentTime,
                                clipStartS,
                                Math.min(
                                  clipEndS - 0.1,
                                  (overlay.visible_until_s ?? clipEndS) - 0.1,
                                ),
                              ),
                            }))
                          }
                        >
                          Playhead
                        </button>
                      </div>
                      <span className="numeric mt-1 block text-[10px] text-ink-600">
                        {formatTimecode(selectedChat.visible_from_s ?? clipStartS)}
                      </span>
                    </label>

                    <label>
                      <span className="text-[11px] text-ink-500">Disappears at</span>
                      <div className="mt-1 flex items-center gap-2">
                        <input
                          type="number"
                          min={Math.min(
                            clipEndS,
                            (selectedChat.visible_from_s ?? clipStartS) + 0.1,
                          )}
                          max={clipEndS}
                          step={0.1}
                          value={selectedChat.visible_until_s ?? clipEndS}
                          onChange={(event) => {
                            const minimum = Math.min(
                              clipEndS,
                              (selectedChat.visible_from_s ?? clipStartS) + 0.1,
                            )
                            updateChatOverlay(selectedChat.id, (overlay) => ({
                              ...overlay,
                              visible_until_s: clamp(
                                Number(event.target.value) || clipEndS,
                                minimum,
                                clipEndS,
                              ),
                            }))
                          }}
                          className="field numeric min-w-0 flex-1 py-1.5"
                        />
                        <button
                          type="button"
                          className="btn btn-ghost px-2 py-1.5 text-[11px]"
                          onClick={() =>
                            updateChatOverlay(selectedChat.id, (overlay) => ({
                              ...overlay,
                              visible_until_s: clamp(
                                currentTime,
                                Math.min(
                                  clipEndS,
                                  (overlay.visible_from_s ?? clipStartS) + 0.1,
                                ),
                                clipEndS,
                              ),
                            }))
                          }
                        >
                          Playhead
                        </button>
                      </div>
                      <span className="numeric mt-1 block text-[10px] text-ink-600">
                        {formatTimecode(selectedChat.visible_until_s ?? clipEndS)}
                      </span>
                    </label>
                  </div>
                  <p className="mt-2 text-[10px] leading-relaxed text-ink-600">
                    Resize chat from the lower-right handle. Chat messages scale from
                    their center so they stay anchored while resizing.
                  </p>
                </div>
              </section>
            )}
            </div>

            <div className={studioTab === 'chat' ? '' : 'hidden'}>
              <p className="eyebrow">Twitch VOD chat</p>
              <p className="mt-1 text-xs leading-relaxed text-ink-500">
                Chat is optional and is never loaded or used for highlight detection unless you
                request it here.
              </p>

              {chatMessages === null ? (
                <button
                  type="button"
                  onClick={() => void loadTwitchChat()}
                  disabled={chatLoading || !onLoadTwitchChat}
                  className="btn btn-primary mt-4 w-full"
                >
                  {chatLoading ? 'Loading chat…' : 'Load Twitch chat'}
                </button>
              ) : (
                <>
                  <div className="mt-3 text-[11px] text-ink-600">
                    {chatMessages.length.toLocaleString()} messages in this short
                  </div>
                  <p className="mt-2 text-[11px] leading-relaxed text-ink-600">
                    Double-click a message to place it in the current layout point.
                  </p>
                  <div className="mt-3 space-y-1">
                    {chatMessages.map((message) => {
                      const added = workingFrame!.chat_overlays.find(
                        (overlay) => overlay.message_id === message.id,
                      )
                      const color = /^#[0-9a-f]{6}$/i.test(message.user_color ?? '')
                        ? message.user_color!
                        : '#9146FF'
                      return (
                        <div
                          key={message.id}
                          onDoubleClick={() => addChatMessage(message)}
                          className={[
                            'cursor-default border px-2.5 py-2 transition-colors',
                            added
                              ? 'border-sodium-700/70 bg-sodium-900/10'
                              : 'border-ink-850 hover:border-ink-700 hover:bg-ink-850/50',
                          ].join(' ')}
                          title="Double-click to add to the frame composer"
                        >
                          <div
                            className="flex items-center gap-1.5"
                            style={{ fontFamily: 'Inter, ui-sans-serif, sans-serif' }}
                          >
                            <span className="numeric mr-1 shrink-0 text-[10px] text-ink-600">
                              {formatTimecode(message.offset_s)}
                            </span>
                            <TwitchBadgeRow clipId={clipId} badges={message.badges} />
                            <span className="truncate text-xs font-bold" style={{ color }}>
                              {message.username}
                            </span>
                            <span className="text-xs text-ink-500">:</span>
                            {added && (
                              <button
                                type="button"
                                className="ml-auto shrink-0 text-[10px] text-signal-bad"
                                onClick={() => removeChatOverlay(added.id)}
                              >
                                remove
                              </button>
                            )}
                          </div>
                          <div
                            className="mt-1 flex flex-wrap items-center gap-x-0.5 text-xs leading-relaxed text-ink-200"
                            style={{ fontFamily: 'Inter, ui-sans-serif, sans-serif' }}
                          >
                            <TwitchFragments clipId={clipId} fragments={message.fragments} fallback={message.message} />
                          </div>
                        </div>
                      )
                    })}
                    {chatMessages.length === 0 && (
                      <p className="border border-ink-850 p-3 text-xs text-ink-600">
                        No chat messages were found during this short.
                      </p>
                    )}
                  </div>
                </>
              )}

              {chatError && (
                <p className="mt-3 border-l-2 border-signal-bad pl-3 text-xs leading-relaxed text-signal-bad">
                  {chatError}
                </p>
              )}
            </div>
          </div>

          <div className="shrink-0 border-t border-ink-800 bg-ink-900 px-4 py-3">
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => void saveDraft()}
                disabled={!dirty || saving || !draft}
                className="btn btn-primary"
              >
                {saving ? 'Saving…' : 'Save layout'}
              </button>
              <button
                type="button"
                onClick={previewMode === 'auto' ? showCustomLayout : showAutoFraming}
                disabled={saving || !draft}
                className={previewMode === 'auto' ? 'btn btn-primary' : 'btn btn-ghost'}
              >
                {previewMode === 'auto' ? 'Return to custom' : 'Auto framing'}
              </button>
            </div>
            <div className="mt-2 flex items-center justify-between gap-3">
              <span className="text-[11px] text-ink-600">
                {previewMode === 'auto'
                  ? 'Auto framing is preview-only; your custom layout is preserved.'
                  : 'Custom layout preview'}
              </span>
              {dirty && (
                <span className="shrink-0 text-[11px] text-sodium-500">
                  Unsaved changes
                </span>
              )}
            </div>
          </div>
        </div>
      </aside>

      <aside className="min-h-0 min-w-0 overflow-hidden border border-ink-800 bg-ink-900/90 xl:col-start-3 xl:row-start-1">
        <div className="flex h-full min-h-0 flex-col p-3">
          <div className="flex shrink-0 items-baseline justify-between gap-3">
            <p className="eyebrow">Frame composer</p>
            <span className="text-[10px] text-ink-600">Source → Output</span>
          </div>

          <div className="mt-2 min-h-0 flex-1 overflow-hidden">
            <div className="flex h-full min-h-0 flex-col gap-3">
              <div className="min-w-0">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                    Source
                  </p>
                  <button
                    type="button"
                    onClick={() => beginSourceSelection('new')}
                    disabled={workingFrame!.overlays.length >= 6}
                    className={`btn px-2.5 py-1.5 text-[11px] ${
                      selectingSourceFor === 'new' ? 'btn-primary' : 'btn-ghost'
                    }`}
                  >
                    {selectingSourceFor === 'new' ? 'Drag region…' : '+ Region'}
                  </button>
                </div>

                <div
                  ref={sourceStage}
                  className={[
                    'relative mx-auto touch-none select-none overflow-hidden border bg-black',
                    selectingSourceFor ? 'cursor-crosshair border-sodium-500' : 'border-ink-700',
                  ].join(' ')}
                  style={{
                    aspectRatio: String(sourceAspect),
                    height: 'min(27vh, 22rem)',
                    width: 'auto',
                    maxWidth: '100%',
                  }}
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
                    <span className="absolute left-1 top-1 bg-black/65 px-1 text-[9px] text-ink-100">
                      BASE
                    </span>
                  </div>

                  {workingFrame!.overlays.map((region, index) => {
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
                          setSelectedChatId(null)
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
                        <span className="absolute left-1 top-1 bg-black/65 px-1 text-[9px] text-sodium-300">
                          {region.label || `R${index + 1}`}
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
                <p className="mt-1 text-center text-[10px] text-ink-600">
                  Drag BASE to reposition · + Region to add an overlay
                </p>
              </div>

              <div className="min-h-0 min-w-0 flex-1">
                <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                  Output
                </p>
                <div
                  ref={outputStage}
                  className="relative mx-auto touch-none select-none overflow-hidden border border-ink-700 bg-black"
                  style={{
                    aspectRatio: String(outputAspect),
                    height:
                      ratio === '9:16'
                        ? 'min(47vh, 34rem)'
                        : ratio === '1:1'
                          ? 'min(36vh, 26rem)'
                          : undefined,
                    width: ratio === '9:16' || ratio === '1:1' ? 'auto' : '100%',
                    maxWidth: '100%',
                  }}
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
                        <span className="pointer-events-none absolute left-1 top-1 bg-black/65 px-1 text-[9px] text-sodium-300">
                          {region.label || `R${index + 1}`}
                        </span>
                        <button
                          type="button"
                          draggable={false}
                          aria-label="Resize overlay"
                          title="Drag to resize · hold Alt to scale from center"
                          onDragStart={(event) => event.preventDefault()}
                          onPointerDown={(event) => beginOutputDrag(event, region, 'resize')}
                          className="absolute -bottom-1.5 -right-1.5 size-5 cursor-nwse-resize touch-none border border-ink-900 bg-sodium-400"
                        />
                      </div>
                    </div>
                  ))}

                  {workingFrame!.chat_overlays.map((chat) => {
                    const color = /^#[0-9a-f]{6}$/i.test(chat.user_color ?? '')
                      ? chat.user_color!
                      : '#9146FF'
                    return (
                      <div
                        key={chat.id}
                        className={[
                          'absolute overflow-visible text-left',
                          selectedChatId === chat.id
                            ? 'border border-sodium-400/80'
                            : 'border border-transparent',
                        ].join(' ')}
                        style={{
                          ...rectStyle(chat.destination),
                          fontFamily: 'Inter, ui-sans-serif, sans-serif',
                        }}
                        onPointerDown={(event) => beginOutputDrag(event, chat, 'move', 'chat')}
                      >
                        <div
                          className="pointer-events-none inline-flex max-w-full flex-col bg-[#18181b]/90 px-1.5 py-1 shadow-sm"
                          style={{ fontSize: `${10 * chatVisualScale(chat.destination)}px` }}
                        >
                          <div className="flex items-center gap-1">
                            <TwitchBadgeRow clipId={clipId} badges={chat.badges} compact />
                            <span className="truncate font-bold" style={{ color }}>
                              {chat.username}
                            </span>
                            <span className="text-white/70">:</span>
                          </div>
                          <div className="mt-0.5 flex max-w-full flex-wrap items-center gap-x-0.5 leading-tight text-white">
                            <TwitchFragments
                              clipId={clipId}
                              fragments={chat.fragments}
                              fallback={chat.message}
                              compact
                            />
                          </div>
                        </div>
                        <button
                          type="button"
                          draggable={false}
                          aria-label="Resize chat message from center"
                          title="Drag to resize from center"
                          onPointerDown={(event) => beginOutputDrag(event, chat, 'resize', 'chat')}
                          className="absolute -bottom-1.5 -right-1.5 size-5 cursor-nwse-resize touch-none border border-ink-900 bg-sodium-400"
                        />
                      </div>
                    )
                  })}
                </div>
                <p className="mt-1 text-center text-[10px] text-ink-600">
                  Drag overlays to move · resize from the lower-right handle
                </p>
              </div>
            </div>
          </div>
        </div>
      </aside>
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
    <details className="border-b border-ink-800 pb-3">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 py-1">
        <div>
          <p className="eyebrow">Presets</p>
          <p className="mt-0.5 text-[11px] text-ink-600">Saved reusable compositions</p>
        </div>
        <span className="numeric text-[11px] text-ink-500">{presets.length}</span>
      </summary>

      <div className="mt-3">
        {presets.length > 0 ? (
          <div className="space-y-2">
            {presets.map((preset) => (
              <div key={preset.id} className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onApply?.(preset)}
                  className="btn btn-ghost min-w-0 flex-1 justify-between px-3 py-1.5"
                >
                  <span className="truncate">{preset.name}</span>
                  <span className="numeric ml-2 shrink-0 text-[10px] text-ink-500">
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
          <p className="text-[11px] text-ink-600">No saved layouts yet.</p>
        )}

        {draft && onSave && onNameChange && (
          <div className="mt-3 flex gap-2">
            <input
              value={name}
              onChange={(event) => onNameChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && name.trim()) {
                  onSave(name.trim(), ratio, draft)
                }
              }}
              placeholder="Preset name"
              className="field min-w-0 flex-1 py-1.5 text-xs"
              maxLength={80}
            />
            <button
              type="button"
              disabled={busy || !name.trim()}
              onClick={() => onSave(name.trim(), ratio, draft)}
              className="btn btn-primary px-3 py-1.5 text-[11px]"
            >
              Save
            </button>
          </div>
        )}
      </div>
    </details>
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
  target: 'region' | 'chat'
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

function cloneChatOverlay(overlay: TwitchChatOverlay): TwitchChatOverlay {
  return {
    ...overlay,
    destination: { ...overlay.destination },
  }
}

function cloneFrame(frame: LayoutFrame): LayoutFrame {
  return {
    base_center_x: frame.base_center_x,
    base_center_y: frame.base_center_y,
    overlays: frame.overlays.map(cloneRegion),
    chat_overlays: frame.chat_overlays.map(cloneChatOverlay),
  }
}

function frameAsManualLayout(frame: LayoutFrame): ManualLayout {
  // Presets are reusable across sources; VOD-specific chat messages are not.
  return { ...cloneFrame(frame), chat_overlays: [], cues: [] }
}

function activeCueIdAtTime(
  layout: ManualLayout | null,
  sourceTime: number,
): string | null {
  if (!layout) return null
  let activeId: string | null = null
  for (const cue of [...layout.cues].sort((a, b) => a.at_s - b.at_s)) {
    if (cue.at_s > sourceTime) break
    activeId = cue.id
  }
  return activeId
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

function twitchAssetSrc(
  clipId: string | undefined,
  assetId: string | null,
  remoteUrl: string | null,
): string | null {
  if (clipId && assetId) return api.twitchChatAssetUrl(clipId, assetId)
  return remoteUrl
}

function TwitchBadgeRow({
  clipId,
  badges,
  compact = false,
}: {
  clipId?: string
  badges: TwitchChatOverlay['badges']
  compact?: boolean
}) {
  if (!badges.length) return null
  const size = compact ? '1.15em' : '1.25em'
  return (
    <span className="inline-flex shrink-0 items-center gap-0.5">
      {badges.map((badge, index) => {
        const src = twitchAssetSrc(clipId, badge.asset_id, badge.image_url)
        if (!src) return null
        return (
          <img
            key={`${badge.set_id}-${badge.version}-${index}`}
            src={src}
            title={badge.title || badge.set_id}
            alt=""
            draggable={false}
            className="inline-block shrink-0 object-contain"
            style={{ width: size, height: size }}
          />
        )
      })}
    </span>
  )
}

function TwitchFragments({
  clipId,
  fragments,
  fallback,
  compact = false,
}: {
  clipId?: string
  fragments: TwitchChatOverlay['fragments']
  fallback: string
  compact?: boolean
}) {
  if (!fragments.length) return <>{fallback}</>
  const size = compact ? '1.6em' : '1.75em'
  return (
    <>
      {fragments.map((fragment, index) => {
        const src = twitchAssetSrc(clipId, fragment.asset_id, fragment.image_url)
        if (fragment.emote_id && src) {
          return (
            <img
              key={`${fragment.emote_id}-${index}`}
              src={src}
              alt={fragment.text}
              title={fragment.text}
              draggable={false}
              className="inline-block shrink-0 object-contain align-middle"
              style={{ width: size, height: size }}
            />
          )
        }
        return <span key={index}>{fragment.text}</span>
      })}
    </>
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
  layout: LayoutFrame,
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

function chatVisualScale(rect: LayoutRect): number {
  return clamp(rect.height / 0.075, 0.55, 2.5)
}

function defaultChatDestination(
  index: number,
  ratio: string,
  message: TwitchChatMessage,
): LayoutRect {
  const badgeSpace = Math.min(0.18, message.badges.length * 0.04)
  const textSpace = Math.min(0.58, Math.max(0.16, message.message.length * 0.008))
  const width = clamp(0.18 + badgeSpace + textSpace, 0.32, ratio === '1:1' ? 0.82 : 0.9)
  const height = ratio === '1:1' ? 0.11 : 0.075
  const x = (1 - width) / 2
  const step = height + 0.02
  const y = clamp(0.8 - (index % 5) * step, 0.04, 1 - height)
  return { x, y, width, height }
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
