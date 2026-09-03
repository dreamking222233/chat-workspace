import { Check, ChevronDown } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

export type SelectMenuOption = { value: string; label: string }

export default function SelectMenu({
  value,
  options,
  onChange,
  ariaLabel,
  placeholder = '请选择',
  disabled = false,
  locked = false,
  onLocked,
  onOpenChange,
  header,
}: {
  value: string
  options: SelectMenuOption[]
  onChange: (value: string) => void
  ariaLabel: string
  placeholder?: string
  disabled?: boolean
  locked?: boolean
  onLocked?: () => void
  onOpenChange?: (open: boolean) => void
  header?: string
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const selected = options.find((item) => item.value === value)
  const label = selected?.label ?? placeholder

  const setMenuOpen = (next: boolean) => {
    setOpen(next)
    onOpenChange?.(next)
  }

  useEffect(() => {
    if (!open) return
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setMenuOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false)
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  useEffect(() => {
    if (locked || disabled) setMenuOpen(false)
  }, [disabled, locked])

  return (
    <div className={`select-menu${open ? ' open' : ''}${locked ? ' locked' : ''}`} ref={rootRef}>
      <button
        type="button"
        className="select-menu-trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => {
          if (disabled) return
          if (locked) {
            setMenuOpen(false)
            onLocked?.()
            return
          }
          setMenuOpen(!open)
        }}
      >
        <span>{label}</span>
        <ChevronDown size={14} />
      </button>
      {open && !locked && (
        <div className="select-menu-panel" role="listbox" aria-label={ariaLabel}>
          {header && (
            <button
              type="button"
              role="option"
              aria-selected={value === ''}
              className={`select-menu-header${value === '' ? ' active' : ''}`}
              onClick={() => {
                onChange('')
                setMenuOpen(false)
              }}
            >
              {header}
            </button>
          )}
          {options.map((item) => {
            const active = item.value === value
            return (
              <button
                type="button"
                role="option"
                aria-selected={active}
                key={item.value || 'empty'}
                className={active ? 'active' : ''}
                onClick={() => {
                  onChange(item.value)
                  setMenuOpen(false)
                }}
              >
                <span className="select-menu-check">{active && <Check size={14} />}</span>
                <span>{item.label}</span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
