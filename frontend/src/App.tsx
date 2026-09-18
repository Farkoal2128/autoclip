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
}: {
  onClick: () => void
  title: string
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className="text-sm font-medium text-ink-400 transition-colors duration-200 hover:text-ink-200"
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
