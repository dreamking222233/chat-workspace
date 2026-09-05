import { Brain, Zap } from 'lucide-react'
import { PointerEvent as ReactPointerEvent, useEffect, useMemo, useRef, useState } from 'react'

export type ReasoningEffort = 'low' | 'medium' | 'high' | 'xhigh'

export const EFFORT_STEPS: Array<{ value: ReasoningEffort | null; label: string }> = [
  { value: null, label: '关闭' },
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
  { value: 'xhigh', label: '超高' },
]

const SPARKS = [
  { x: 6, y: 30, s: 2, d: '0s' },
  { x: 12, y: 62, s: 2.5, d: '.28s' },
  { x: 18, y: 22, s: 2, d: '.9s' },
  { x: 24, y: 70, s: 3, d: '.15s' },
  { x: 31, y: 38, s: 2, d: '1.1s' },
  { x: 37, y: 16, s: 2.5, d: '.55s' },
  { x: 43, y: 58, s: 2, d: '.05s' },
  { x: 49, y: 28, s: 3, d: '1.35s' },
  { x: 55, y: 72, s: 2, d: '.4s' },
  { x: 61, y: 18, s: 2.5, d: '.8s' },
  { x: 67, y: 48, s: 2, d: '1.2s' },
  { x: 73, y: 66, s: 3, d: '.22s' },
  { x: 79, y: 24, s: 2, d: '.7s' },
  { x: 85, y: 54, s: 2.5, d: '1.05s' },
  { x: 91, y: 36, s: 2, d: '.5s' },
  { x: 95, y: 68, s: 2, d: '1.45s' },
]

const ACCENT_STOPS: Array<[number, number[]]> = [
  [0, [142, 142, 147]],
  [0.25, [79, 110, 247]],
  [0.5, [109, 94, 252]],
  [0.75, [139, 92, 246]],
  [1, [176, 108, 255]],
]

function mix(a: number[], b: number[], t: number) {
  return a.map((value, index) => Math.round(value + (b[index] - value) * t))
}

function accentColor(ratio: number) {
  const clamped = Math.min(1, Math.max(0, ratio))
  let index = 1
  while (index < ACCENT_STOPS.length && clamped > ACCENT_STOPS[index][0]) index += 1
  const [startAt, start] = ACCENT_STOPS[index - 1]
  const [endAt, end] = ACCENT_STOPS[index]
  const t = endAt === startAt ? 0 : (clamped - startAt) / (endAt - startAt)
  const [r, g, b] = mix(start, end, t)
  return `rgb(${r}, ${g}, ${b})`
}

function stepIndex(value: ReasoningEffort | null) {
  const index = EFFORT_STEPS.findIndex((item) => item.value === value)
  return index < 0 ? 0 : index
}

export default function EffortSlider({
  value,
  open,
  disabled = false,
  onChange,
  onOpenChange,
}: {
  value: ReasoningEffort | null
  open: boolean
  disabled?: boolean
  onChange: (value: ReasoningEffort | null) => void
  onOpenChange: (open: boolean) => void
}) {
  const rootRef = useRef<HTMLDivElement>(null)
  const trackRef = useRef<HTMLDivElement>(null)
  const sliderRef = useRef<HTMLDivElement>(null)
  const draggingRef = useRef(false)
  const moveRef = useRef<(clientX: number) => void>(() => {})
  const [dragRatio, setDragRatio] = useState<number | null>(null)
  const committed = stepIndex(value)
  const maxIndex = EFFORT_STEPS.length - 1
  const visualRatio = Number.isFinite(dragRatio) ? dragRatio as number : committed / maxIndex
  const nearest = Math.min(maxIndex, Math.max(0, Math.round(visualRatio * maxIndex)))
  const current = EFFORT_STEPS[nearest] ?? EFFORT_STEPS[committed]
  const accent = useMemo(() => accentColor(visualRatio), [visualRatio])
  const fillPercent = Math.max(visualRatio * 100, 12)

  const ratioFromClientX = (clientX: number, target?: EventTarget | null) => {
    if (!Number.isFinite(clientX)) return null
    const el = (trackRef.current ?? target) as HTMLElement | null
    const rect = el?.getBoundingClientRect()
    if (!rect || rect.width <= 0) return null
    return Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
  }

  const commitRatio = (ratio: number) => {
    const index = Math.min(maxIndex, Math.max(0, Math.round(ratio * maxIndex)))
    onChange(EFFORT_STEPS[index].value)
  }

  moveRef.current = (clientX: number) => {
    const ratio = ratioFromClientX(clientX)
    if (ratio == null) return
    setDragRatio(ratio)
    commitRatio(ratio)
  }

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button > 0) return
    const ratio = ratioFromClientX(event.clientX, event.currentTarget)
    if (ratio == null) return
    event.preventDefault()
    draggingRef.current = true
    event.currentTarget.setPointerCapture?.(event.pointerId)
    setDragRatio(ratio)
    commitRatio(ratio)
  }

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return
    const ratio = ratioFromClientX(event.clientX, event.currentTarget)
    if (ratio == null) return
    setDragRatio(ratio)
    commitRatio(ratio)
  }

  const endDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return
    draggingRef.current = false
    const ratio = ratioFromClientX(event.clientX, event.currentTarget)
    if (ratio != null) commitRatio(ratio)
    setDragRatio(null)
    try { event.currentTarget.releasePointerCapture?.(event.pointerId) } catch { /* Capture may already be released. */ }
  }

  useEffect(() => {
    if (disabled) onOpenChange(false)
  }, [disabled, onOpenChange])

  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      if (!draggingRef.current) return
      moveRef.current(event.clientX)
    }
    const onUp = (event: PointerEvent) => {
      if (!draggingRef.current) return
      draggingRef.current = false
      moveRef.current(event.clientX)
      setDragRatio(null)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
    }
  }, [])

  useEffect(() => {
    if (!open || disabled) return
    sliderRef.current?.focus({ preventScroll: true })
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) onOpenChange(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onOpenChange(false)
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [disabled, open, onOpenChange])

  return (
    <div className={`effort-wrap${open ? ' open' : ''}`} ref={rootRef}>
      <button
        type="button"
        className={`think-button effort-trigger${value ? ' active' : ''}`}
        aria-label={`思考等级：${EFFORT_STEPS[committed].label}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        disabled={disabled}
        onClick={() => { if (!disabled) onOpenChange(!open) }}
      >
        <Brain size={19} />
        <span className="effort-trigger-text">
          <span className="effort-trigger-sizer" aria-hidden="true">思考 · 超高</span>
          <span className="effort-trigger-value">{value ? `思考 · ${EFFORT_STEPS[committed].label}` : '思考'}</span>
        </span>
      </button>
      {open && !disabled && (
        <div className={`effort-popover${dragRatio !== null ? ' dragging' : ''}`} role="dialog" aria-label="选择思考等级" style={{ ['--effort-glow' as string]: String(visualRatio) }}>
          <div className="effort-head">
            <Zap className="effort-head-icon" size={16} strokeWidth={2.15} />
            <div className="effort-head-title">
              {current.value ? <>思考 <em style={{ color: accent }}>{current.label}</em></> : '关闭思考'}
            </div>
          </div>
          <div
            ref={sliderRef}
            className="effort-slider"
            role="slider"
            tabIndex={0}
            aria-label="选择思考等级"
            aria-valuemin={0}
            aria-valuemax={maxIndex}
            aria-valuenow={nearest}
            aria-valuetext={current.label}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onKeyDown={(event) => {
              if (event.key === 'ArrowRight' || event.key === 'ArrowUp') {
                event.preventDefault()
                onChange(EFFORT_STEPS[Math.min(maxIndex, committed + 1)].value)
              } else if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') {
                event.preventDefault()
                onChange(EFFORT_STEPS[Math.max(0, committed - 1)].value)
              } else if (event.key === 'Home') {
                event.preventDefault()
                onChange(EFFORT_STEPS[0].value)
              } else if (event.key === 'End') {
                event.preventDefault()
                onChange(EFFORT_STEPS[maxIndex].value)
              }
            }}
          >
            <div className="effort-track" ref={trackRef}>
              <div className="effort-fill" style={{ width: `${fillPercent}%` }}>
                <span className="effort-fill-spectrum" aria-hidden="true" />
                <span className="effort-fill-sheen" aria-hidden="true" />
                <span className="effort-sparks" aria-hidden="true">
                  {SPARKS.map((spark, index) => (
                    <i
                      key={index}
                      style={{
                        left: `${spark.x}%`,
                        top: `${spark.y}%`,
                        width: spark.s,
                        height: spark.s,
                        animationDelay: spark.d,
                        opacity: 0.25 + visualRatio * 0.7,
                      }}
                    />
                  ))}
                </span>
              </div>
            </div>
            <div className="effort-thumb" style={{ left: `calc(${visualRatio} * (100% - 28px))` }} />
          </div>
        </div>
      )}
    </div>
  )
}
