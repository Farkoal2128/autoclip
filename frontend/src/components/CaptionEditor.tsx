import { useEffect, useState } from 'react'

import { formatTimecode, type Word } from '../api'

const MIN_INSERT_WORD_S = 0.08
const PREFERRED_INSERT_WORD_S = 0.24

/**
 * Word-level caption editing.
 *
 * Existing words keep Whisper's measured timings. Inserted words are given a
 * short inferred timing slot between their neighbours (or borrowed from the
 * nearest neighbour when there is no gap), so additions stay renderable without
 * asking the user to hand-edit timestamps.
 */
export function CaptionEditor({
  words,
  clipStartS,
  clipEndS,
  onChange,
  onSave,
  saving,
  dirty,
}: {
  words: Word[]
  clipStartS: number
  clipEndS: number
  onChange: (words: Word[]) => void
  onSave: () => void
  saving: boolean
  dirty: boolean
}) {
  const [editing, setEditing] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  const [insertingAfter, setInsertingAfter] = useState<number | null>(null)
  const [insertDraft, setInsertDraft] = useState('')

  useEffect(() => setEditing(null), [words.length])

  const commit = (index: number) => {
    const text = draft.trim()
    if (!text) {
      onChange(words.filter((_, i) => i !== index))
    } else if (text !== words[index].text) {
      const next = words.map((word, i) => (i === index ? { ...word, text } : word))
      onChange(next)
    }
    setEditing(null)
  }

  const beginInsert = (afterIndex: number) => {
    setEditing(null)
    setInsertDraft('')
    setInsertingAfter(afterIndex)
  }

  const cancelInsert = () => {
    setInsertDraft('')
    setInsertingAfter(null)
  }

  const commitInsert = () => {
    if (insertingAfter === null) return
    const tokens = insertDraft.trim().split(/\s+/).filter(Boolean)
    if (tokens.length === 0) {
      cancelInsert()
      return
    }

    onChange(insertTimedWords(words, insertingAfter, tokens, clipStartS, clipEndS))
    cancelInsert()
  }

  return (
    <div>
      <div className="flex items-baseline justify-between border-b border-ink-800 pb-2">
        <span className="eyebrow">Transcript</span>
        <span className="numeric text-xs text-ink-600">{words.length} words</span>
      </div>

      {words.length === 0 && insertingAfter === null ? (
        <div className="mt-4">
          <p className="text-sm text-ink-500">No caption words remain for this clip.</p>
          <button onClick={() => beginInsert(-1)} className="btn btn-ghost mt-3">
            Add word
          </button>
        </div>
      ) : (
        <>
          {/* Words flow as text, not rows. The small + insertion points let an
              omitted Whisper word be restored at the exact place it belongs. */}
          <div className="mt-4 max-h-[38vh] overflow-y-auto pr-1 text-[0.9375rem] leading-[1.9]">
            <InsertPoint
              visible={words.length > 0}
              active={insertingAfter === -1}
              draft={insertDraft}
              label="Insert before the first word"
              onBegin={() => beginInsert(-1)}
              onDraft={setInsertDraft}
              onCommit={commitInsert}
              onCancel={cancelInsert}
            />

            {words.map((word, index) => (
              <span key={`${word.start}-${word.end}-${index}`} className="inline">
                {editing === index ? (
                  <input
                    autoFocus
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    onBlur={() => commit(index)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') commit(index)
                      if (event.key === 'Escape') setEditing(null)
                      if (event.key === 'Tab') {
                        event.preventDefault()
                        commit(index)
                        const next = index + (event.shiftKey ? -1 : 1)
                        if (next >= 0 && next < words.length) {
                          setDraft(words[next].text)
                          setEditing(next)
                        }
                      }
                    }}
                    className="mr-1 w-[8ch] border-b border-sodium-500 bg-transparent px-0.5 text-ink-100 outline-none"
                    style={{ width: `${Math.max(4, draft.length + 1)}ch` }}
                  />
                ) : (
                  <button
                    onClick={() => {
                      setInsertingAfter(null)
                      setDraft(word.text)
                      setEditing(index)
                    }}
                    title={`Edit word · ${formatTimecode(word.start)}`}
                    className="mr-0.5 rounded-[2px] px-0.5 text-ink-200 transition-colors duration-150 hover:bg-sodium-700/25 hover:text-ink-100"
                  >
                    {word.text}
                  </button>
                )}

                <InsertPoint
                  visible
                  active={insertingAfter === index}
                  draft={insertDraft}
                  label={`Insert after ${word.text}`}
                  onBegin={() => beginInsert(index)}
                  onDraft={setInsertDraft}
                  onCommit={commitInsert}
                  onCancel={cancelInsert}
                />
              </span>
            ))}
          </div>

          {insertingAfter === null && (
            <button
              type="button"
              onClick={() => beginInsert(words.length - 1)}
              className="btn btn-quiet mt-2 -ml-1"
            >
              + Add at end
            </button>
          )}
        </>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-4">
        <button onClick={onSave} disabled={!dirty || saving} className="btn btn-ghost">
          {saving ? 'Saving…' : 'Save edits'}
        </button>
        <span className="text-xs text-ink-500">
          {dirty
            ? 'Unsaved changes'
            : 'Click a word to edit · clear it to remove · use + to insert'}
        </span>
      </div>
    </div>
  )
}

function InsertPoint({
  visible,
  active,
  draft,
  label,
  onBegin,
  onDraft,
  onCommit,
  onCancel,
}: {
  visible: boolean
  active: boolean
  draft: string
  label: string
  onBegin: () => void
  onDraft: (value: string) => void
  onCommit: () => void
  onCancel: () => void
}) {
  if (!visible && !active) return null

  if (active) {
    return (
      <input
        autoFocus
        value={draft}
        placeholder="new word"
        aria-label="New transcript word or phrase"
        onChange={(event) => onDraft(event.target.value)}
        onBlur={() => (draft.trim() ? onCommit() : onCancel())}
        onKeyDown={(event) => {
          if (event.key === 'Enter') onCommit()
          if (event.key === 'Escape') onCancel()
        }}
        className="mr-1 w-[9ch] border-b border-sodium-500 bg-transparent px-0.5 text-sodium-400 outline-none"
        style={{ width: `${Math.max(7, draft.length + 2)}ch` }}
      />
    )
  }

  return (
    <button
      type="button"
      onClick={onBegin}
      aria-label={label}
      title={label}
      className="mr-0.5 inline-flex size-4 translate-y-[1px] items-center justify-center rounded-sm text-[11px] leading-none text-ink-700 transition-colors hover:bg-ink-850 hover:text-sodium-500 focus:text-sodium-500"
    >
      +
    </button>
  )
}

function insertTimedWords(
  words: Word[],
  afterIndex: number,
  tokens: string[],
  clipStartS: number,
  clipEndS: number,
): Word[] {
  const next = words.map((word) => ({ ...word }))
  const insertAt = Math.max(0, Math.min(next.length, afterIndex + 1))
  const left = insertAt > 0 ? next[insertAt - 1] : null
  const right = insertAt < next.length ? next[insertAt] : null
  const count = tokens.length

  const minimum = MIN_INSERT_WORD_S * count
  const preferred = PREFERRED_INSERT_WORD_S * count
  let slotStart = left?.end ?? clipStartS
  let slotEnd = right?.start ?? clipEndS
  const gap = slotEnd - slotStart

  if (gap >= minimum) {
    const duration = Math.min(gap, Math.max(minimum, preferred))
    if (left && right) {
      const centre = (slotStart + slotEnd) / 2
      slotStart = centre - duration / 2
      slotEnd = centre + duration / 2
    } else if (right) {
      slotStart = Math.max(clipStartS, slotEnd - duration)
    } else {
      slotEnd = Math.min(clipEndS, slotStart + duration)
    }
  } else if (right && right.end - right.start >= preferred + MIN_INSERT_WORD_S) {
    // Whisper occasionally absorbs an omitted short word into the following
    // word's timing. Borrow a small leading slice so events never overlap.
    slotStart = right.start
    slotEnd = right.start + preferred
    right.start = slotEnd
  } else if (left && left.end - left.start >= preferred + MIN_INSERT_WORD_S) {
    // Same fallback in the other direction when the following word is too short.
    slotEnd = left.end
    slotStart = left.end - preferred
    left.end = slotStart
  } else {
    // Extremely dense timings leave no clean room to borrow. Keep the inferred
    // slot short and clamped rather than creating an invalid negative duration.
    const boundary = right?.start ?? left?.end ?? clipStartS
    const duration = Math.min(
      Math.max(minimum, 0.04 * count),
      Math.max(0.04 * count, clipEndS - clipStartS),
    )
    slotStart = Math.max(clipStartS, Math.min(boundary, clipEndS - duration))
    slotEnd = Math.min(clipEndS, slotStart + duration)
  }

  const total = Math.max(0.001 * count, slotEnd - slotStart)
  const step = total / count
  const speaker = left?.speaker ?? right?.speaker ?? null
  const inserted = tokens.map((text, index) => ({
    text,
    start: slotStart + step * index,
    end: index === count - 1 ? slotEnd : slotStart + step * (index + 1),
    speaker,
  }))

  next.splice(insertAt, 0, ...inserted)
  return next
}
