import { useEffect, useState } from 'react'

import { formatTimecode, type Word } from '../api'

const PREFERRED_WORD_S = 0.24

/**
 * Word-level caption editing.
 *
 * Each visible token is one timed caption word. Editing a token to contain
 * whitespace automatically replaces it with multiple timed words, so users can
 * insert missing text by typing naturally instead of using separate "+" controls.
 * Clearing a token removes it.
 */
export function CaptionEditor({
  words,
  clipStartS,
  clipEndS,
  onChange,
  onSeek,
  onSave,
  saving,
  dirty,
}: {
  words: Word[]
  clipStartS: number
  clipEndS: number
  onChange: (words: Word[]) => void
  onSeek?: (sourceTime: number) => void
  onSave: () => void
  saving: boolean
  dirty: boolean
}) {
  const [editing, setEditing] = useState<number | null>(null)
  const [draft, setDraft] = useState('')

  useEffect(() => setEditing(null), [words.length])

  const beginEdit = (index: number) => {
    setDraft(words[index].text)
    setEditing(index)
  }

  const commit = (index: number) => {
    const tokens = splitWords(draft)

    if (tokens.length === 0) {
      onChange(words.filter((_, wordIndex) => wordIndex !== index))
    } else if (tokens.length === 1) {
      if (tokens[0] !== words[index].text) {
        onChange(
          words.map((word, wordIndex) =>
            wordIndex === index ? { ...word, text: tokens[0] } : word,
          ),
        )
      }
    } else {
      onChange(replaceWithTimedWords(words, index, tokens, clipStartS, clipEndS))
    }

    setEditing(null)
  }

  const commitEmptyTranscript = () => {
    const tokens = splitWords(draft)
    if (tokens.length === 0) return
    onChange(makeInitialTimedWords(tokens, clipStartS, clipEndS))
    setDraft('')
  }

  return (
    <div>
      <div className="flex items-baseline justify-between border-b border-ink-800 pb-2">
        <span className="eyebrow">Transcript</span>
        <span className="numeric text-xs text-ink-600">{words.length} words</span>
      </div>

      {words.length === 0 ? (
        <div className="mt-4">
          <p className="text-sm text-ink-500">
            No caption words remain. Type text below to create caption words again.
          </p>
          <input
            value={draft}
            placeholder="Type words separated by spaces…"
            onChange={(event) => setDraft(event.target.value)}
            onBlur={commitEmptyTranscript}
            onKeyDown={(event) => {
              if (event.key === 'Enter') commitEmptyTranscript()
              if (event.key === 'Escape') setDraft('')
            }}
            className="field mt-3 text-sm"
          />
        </div>
      ) : (
        <div className="mt-4 max-h-[38vh] overflow-y-auto pr-1 text-[0.9375rem] leading-[1.9]">
          {words.map((word, index) =>
            editing === index ? (
              <input
                key={`${word.start}-${word.end}-${index}`}
                autoFocus
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onBlur={() => commit(index)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') commit(index)
                  if (event.key === 'Escape') setEditing(null)
                }}
                className="mr-1 w-[8ch] border-b border-sodium-500 bg-transparent px-0.5 text-ink-100 outline-none"
                style={{ width: `${Math.max(4, draft.length + 1)}ch` }}
              />
            ) : (
              <button
                key={`${word.start}-${word.end}-${index}`}
                onClick={() => onSeek?.(word.start)}
                onDoubleClick={() => beginEdit(index)}
                title={`Seek preview · double-click to edit · ${formatTimecode(word.start)}`}
                className="mr-1 rounded-[2px] px-0.5 text-ink-200 transition-colors duration-150 hover:bg-sodium-700/25 hover:text-ink-100"
              >
                {word.text}
              </button>
            ),
          )}
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-4">
        <button onClick={onSave} disabled={!dirty || saving} className="btn btn-ghost">
          {saving ? 'Saving…' : 'Save edits'}
        </button>
        <span className="text-xs text-ink-500">
          {dirty
            ? 'Unsaved changes'
            : 'Click a word to seek · double-click to edit · clear it to remove · type spaces to add words'}
        </span>
      </div>
    </div>
  )
}

function splitWords(text: string): string[] {
  return text.trim().split(/\s+/).filter(Boolean)
}

function replaceWithTimedWords(
  words: Word[],
  index: number,
  tokens: string[],
  clipStartS: number,
  clipEndS: number,
): Word[] {
  const original = words[index]
  const previous = index > 0 ? words[index - 1] : null
  const following = index + 1 < words.length ? words[index + 1] : null

  const availableStart = Math.max(clipStartS, previous?.end ?? clipStartS)
  const availableEnd = Math.min(clipEndS, following?.start ?? clipEndS)
  const availableDuration = Math.max(0.001, availableEnd - availableStart)
  const originalDuration = Math.max(0.001, original.end - original.start)
  const desiredDuration = Math.min(
    availableDuration,
    Math.max(originalDuration, PREFERRED_WORD_S * tokens.length),
  )

  const originalMidpoint = (original.start + original.end) / 2
  let start = originalMidpoint - desiredDuration / 2
  start = Math.max(availableStart, Math.min(start, availableEnd - desiredDuration))
  const end = start + desiredDuration

  const step = (end - start) / tokens.length
  const replacement = tokens.map((text, tokenIndex) => ({
    text,
    start: start + step * tokenIndex,
    end: tokenIndex === tokens.length - 1 ? end : start + step * (tokenIndex + 1),
    speaker: original.speaker,
  }))

  return [...words.slice(0, index), ...replacement, ...words.slice(index + 1)]
}

function makeInitialTimedWords(
  tokens: string[],
  clipStartS: number,
  clipEndS: number,
): Word[] {
  const available = Math.max(0.001, clipEndS - clipStartS)
  const total = Math.min(available, Math.max(PREFERRED_WORD_S * tokens.length, 0.3))
  const step = total / tokens.length

  return tokens.map((text, index) => ({
    text,
    start: clipStartS + step * index,
    end: index === tokens.length - 1 ? clipStartS + total : clipStartS + step * (index + 1),
    speaker: null,
  }))
}
