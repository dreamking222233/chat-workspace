// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import EffortSlider, { type ReasoningEffort } from './EffortSlider'

function Harness({
  disabled = false,
  initial = null as ReasoningEffort | null,
}: {
  disabled?: boolean
  initial?: ReasoningEffort | null
}) {
  const [value, setValue] = useState<ReasoningEffort | null>(initial)
  const [open, setOpen] = useState(false)
  return <EffortSlider value={value} open={open} disabled={disabled} onChange={setValue} onOpenChange={setOpen} />
}

function mockTrack(width = 240) {
  const slider = screen.getByRole('slider', { name: '选择思考等级' })
  const rect = {
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: width,
    bottom: 26,
    width,
    height: 26,
    toJSON() { return {} },
  } as DOMRect
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(rect)
  return slider
}

describe('EffortSlider', () => {
  beforeEach(() => {
    HTMLElement.prototype.setPointerCapture = vi.fn()
    HTMLElement.prototype.releasePointerCapture = vi.fn()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('reserves the longest trigger label so the control width does not jump', () => {
    render(<Harness />)
    expect(screen.getByText('思考 · 超高', { selector: '.effort-trigger-sizer' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '思考等级：关闭' }))
    fireEvent.keyDown(screen.getByRole('slider', { name: '选择思考等级' }), { key: 'End' })
    expect(screen.getByRole('button', { name: '思考等级：超高' })).toBeTruthy()
    expect(screen.getByText('思考 · 超高', { selector: '.effort-trigger-sizer' })).toBeTruthy()
  })

  it('opens a slider and reports keyboard steps', () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: '思考等级：关闭' }))
    const slider = screen.getByRole('slider', { name: '选择思考等级' })
    expect(slider.getAttribute('aria-valuetext')).toBe('关闭')
    expect(screen.getByText('关闭思考')).toBeTruthy()
    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    expect(slider.getAttribute('aria-valuetext')).toBe('低')
    expect(screen.getByRole('button', { name: '思考等级：低' })).toBeTruthy()
    fireEvent.keyDown(slider, { key: 'End' })
    expect(slider.getAttribute('aria-valuetext')).toBe('超高')
    expect(screen.getByText('超高')).toBeTruthy()
    fireEvent.keyDown(slider, { key: 'Home' })
    expect(slider.getAttribute('aria-valuetext')).toBe('关闭')
  })

  it('snaps a drag on the track to the nearest effort', () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: '思考等级：关闭' }))
    const slider = mockTrack(240)
    const pointer = (type: string, clientX: number) => {
      const event = new Event(type, { bubbles: true, cancelable: true })
      Object.assign(event, { clientX, button: 0, buttons: type === 'pointerup' ? 0 : 1, pointerId: 1 })
      return event
    }
    fireEvent(slider, pointer('pointerdown', 240))
    fireEvent(slider, pointer('pointerup', 240))
    expect(screen.getByRole('button', { name: '思考等级：超高' })).toBeTruthy()
    fireEvent(slider, pointer('pointerdown', 90))
    fireEvent(slider, pointer('pointermove', 90))
    fireEvent(slider, pointer('pointerup', 90))
    expect(screen.getByRole('button', { name: '思考等级：中' })).toBeTruthy()
  })

  it('does not open when disabled', () => {
    render(<Harness disabled />)
    fireEvent.click(screen.getByRole('button', { name: '思考等级：关闭' }))
    expect(screen.queryByRole('slider', { name: '选择思考等级' })).toBeNull()
    expect(screen.queryByRole('dialog', { name: '选择思考等级' })).toBeNull()
  })

  it('closes on outside click and Escape', () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: '思考等级：关闭' }))
    expect(screen.getByRole('slider', { name: '选择思考等级' })).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('slider', { name: '选择思考等级' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '思考等级：关闭' }))
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('slider', { name: '选择思考等级' })).toBeNull()
  })
})
