import { useEffect, useState } from 'react'

import { formatTimecode, type CutRange, type Word } from '../api'

const MIN_CUT_S = 0.1
const SNAP_DISTANCE_S = 0.75

/** Non-destructive middle-cut editor driven by the preview playhead. */
export function CutEditor({
  cuts,
  currentTime,
  startS,
  endS,
  words,
  busy,
  onChange,
}: {
  cuts: CutRange[]
  currentTime: number
  startS: number
  endS: number
  words: Word[]
  busy: boolean
  onChange: (cuts: CutRange[]) => void
}) {
  const [markStart, setMarkStart] = useState<number | null>(null)

  useEffect(() => setMarkStart(null), [startS, endS])

  const mark = () => {
    const snapped = snapBoundary(currentTime, words, 'start')
    setMarkStart(Math.max(startS + 0.05, Math.min(endS - 0.15, snapped)))
  }

  const cutToPlayhead = () => {
    if (markStart === null) return
    const snappedEnd = snapBoundary(currentTime, words, 'end')
    const cutEnd = Math.min(endS - 0.05, snappedEnd)
    if (cutEnd - markStart < MIN_CUT_S) return

    onChange(normalise([...cuts, { start_s: markStart, end_s: cutEnd }]))
    setMarkStart(null)
  }

  return (
    <div>
      <div className="flex items-baseline justify-between border-b border-ink-800 pb-2">
        <span className="eyebrow">Middle cuts</span>
        <span className="numeric text-xs text-ink-600">
          playhead {formatTimecode(Math.max(0, currentTime - startS))}
        </span>
      </div>

      <p className="mt-3 text-xs leading-relaxed text-ink-500">
        Pause at the start of a boring section, mark it, move forward, then cut to the
        playhead. Cuts snap to nearby word boundaries and are skipped in preview and export.
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {markStart === null ? (
          <button
            type="button"
            onClick={mark}
            disabled={busy || currentTime <= startS + 0.05 || currentTime >= endS - 0.15}
            className="btn btn-ghost"
          >
            Mark cut start
          </button>
        ) : (
          <>
            <span className="numeric text-xs text-sodium-500">
              from {formatTimecode(markStart - startS)}
            </span>
            <button
              type="button"
              onClick={cutToPlayhead}
              disabled={busy || currentTime - markStart < MIN_CUT_S}
              className="btn btn-primary"
            >
              Cut to playhead
            </button>
            <button
              type="button"
              onClick={() => setMarkStart(null)}
              disabled={busy}
              className="btn btn-quiet"
            >
              Cancel
            </button>
          </>
        )}
      </div>

      {cuts.length > 0 && (
        <ul className="mt-4 space-y-2">
          {cuts.map((cut, index) => (
            <li
              key={`${cut.start_s}-${cut.end_s}-${index}`}
              className="flex items-center justify-between gap-3 border-l-2 border-ink-700 pl-3 text-xs"
            >
              <span className="numeric text-ink-400">
                {formatTimecode(cut.start_s - startS)} → {formatTimecode(cut.end_s - startS)}
                <span className="ml-2 text-ink-600">
                  −{(cut.end_s - cut.start_s).toFixed(1)}s
                </span>
              </span>
              <button
                type="button"
                disabled={busy}
                onClick={() => onChange(cuts.filter((_, i) => i !== index))}
                className="text-ink-400 hover:text-signal-good"
              >
                restore
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function snapBoundary(time: number, words: Word[], edge: 'start' | 'end'): number {
  if (words.length === 0) return time

  const candidates = words.map((word) => (edge === 'start' ? word.start : word.end))
  let best = candidates[0]
  for (const candidate of candidates) {
    if (Math.abs(candidate - time) < Math.abs(best - time)) best = candidate
  }
  return Math.abs(best - time) <= SNAP_DISTANCE_S ? best : time
}

function normalise(cuts: CutRange[]): CutRange[] {
  const ordered = [...cuts].sort((a, b) => a.start_s - b.start_s)
  const merged: CutRange[] = []
  for (const cut of ordered) {
    const previous = merged[merged.length - 1]
    if (previous && cut.start_s <= previous.end_s + 0.01) {
      previous.end_s = Math.max(previous.end_s, cut.end_s)
    } else {
      merged.push({ ...cut })
    }
  }
  return merged
}
