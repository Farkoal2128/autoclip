import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'

import { api, type SystemStatus } from './api'

/**
 * Application shell.
 *
 * A masthead rather than a sidebar: this is a four-screen tool, and a
 * persistent nav rail would spend a fifth of the width restating that.
 */
export function App() {
  const [system, setSystem] = useState<SystemStatus | null>(null)
  const [quitting, setQuitting] = useState(false)
  const [stopped, setStopped] = useState(false)

  useEffect(() => {
    api.system().then(setSystem).catch(() => setSystem(null))
  }, [])

  const openLocation = async (location: 'data' | 'install') => {
    try {
      await api.openLocation(location)
    } catch (error) {
      window.alert(error instanceof Error ? error.message : 'Could not open the requested folder.')
    }
  }

  const quitAutoClip = async () => {
    if (
      !window.confirm(
        'Quit AutoClip? This stops the local server and cancels any active or queued work. You can reopen it from the desktop shortcut.',
      )
    ) {
      return
    }

    setQuitting(true)
    try {
      await api.shutdown()
      setStopped(true)
    } catch (error) {
      window.alert(error instanceof Error ? error.message : 'Could not stop AutoClip.')
    } finally {
      setQuitting(false)
    }
  }

  if (stopped) {
    return (
      <div className="min-h-screen bg-ink-900 px-6 py-24">
        <div className="mx-auto max-w-xl border border-ink-800 bg-ink-850/40 p-8">
          <p className="eyebrow text-signal-good">AutoClip stopped</p>
          <h1 className="mt-3 font-display text-4xl text-ink-100">The local server has shut down.</h1>
          <p className="mt-4 text-sm leading-relaxed text-ink-400">
            You can close this tab. To use AutoClip again, double-click the desktop shortcut or
            start it from PowerShell.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-ink-900">
      <header className="border-b border-ink-800">
        <div className="mx-auto flex max-w-[1600px] items-baseline gap-8 px-6 py-4 lg:px-10">
          <NavLink to="/" className="group flex items-baseline gap-2.5">
            <span className="font-display text-2xl leading-none text-ink-100">
              Auto<span className="italic text-sodium-500">Clip</span>
            </span>
          </NavLink>

          <nav className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
            <TopLink to="/" end>
              New
            </TopLink>
            <TopLink to="/settings">Settings</TopLink>
            <TopAction
              onClick={() => void openLocation('data')}
              title="Open the .autoclip folder containing exports, work, media, and config.json"
            >
              Open local files
            </TopAction>
            <TopAction
              onClick={() => void openLocation('install')}
              title="Open the AutoClip source/install folder containing backend and frontend"
            >
              Open install location
            </TopAction>
            <TopAction
              onClick={() => void quitAutoClip()}
              title="Stop the local AutoClip server"
              disabled={quitting}
              danger
            >
              {quitting ? 'Quitting…' : 'Quit AutoClip'}
            </TopAction>
          </nav>

          <div className="ml-auto flex items-baseline gap-5">
            {system && <SystemBadge system={system} />}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1600px] px-6 pb-24 lg:px-10">
        <Outlet />
      </main>
    </div>
  )
}

function TopLink({
  to,
  end,
  children,
}: {
  to: string
  end?: boolean
  children: React.ReactNode
}) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        [
          'text-sm font-medium transition-colors duration-200',
          isActive ? 'text-sodium-500' : 'text-ink-400 hover:text-ink-200',
        ].join(' ')
      }
    >
      {children}
    </NavLink>
  )
}

function TopAction({
  onClick,
  title,
  children,
  disabled = false,
  danger = false,
}: {
  onClick: () => void
  title: string
  children: React.ReactNode
  disabled?: boolean
  danger?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      disabled={disabled}
      className={[
        'text-sm font-medium transition-colors duration-200 disabled:cursor-not-allowed disabled:opacity-50',
        danger ? 'text-ink-500 hover:text-signal-bad' : 'text-ink-400 hover:text-ink-200',
      ].join(' ')}
    >
      {children}
    </button>
  )
}

/**
 * Compact machine status.
 *
 * Surfaced permanently rather than hidden in settings because the two things it
 * reports — whether ffmpeg can render, and whether the GPU is being used — are
 * the two that change how long everything takes.
 */
function SystemBadge({ system }: { system: SystemStatus }) {
  const accel = system.accel.toUpperCase()
  return (
    <div className="hidden items-baseline gap-4 text-xs md:flex">
      <span className="numeric text-ink-400">
        {accel}
        {system.gpu_name && accel === 'CUDA' && (
          <span className="text-ink-600"> · {system.gpu_name.replace('NVIDIA GeForce ', '')}</span>
        )}
      </span>
      <span
        className={system.ready ? 'text-ink-400' : 'text-signal-bad'}
        title={system.ready ? 'All required components present' : 'Run autoclip doctor'}
      >
        {system.ready ? 'ready' : 'not ready'}
      </span>
    </div>
  )
}
