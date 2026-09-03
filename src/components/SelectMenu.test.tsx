// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SelectMenu from './SelectMenu'

const options = [
  { value: '', label: '自动模型' },
  { value: 'channel-1::gpt', label: 'gpt · 渠道1 · 官网版' },
]

describe('SelectMenu', () => {
  afterEach(cleanup)

  it('opens a custom list and reports the chosen value', () => {
    const onChange = vi.fn()
    render(<SelectMenu ariaLabel="选择模型" value="" options={options} onChange={onChange} header="自动模型" />)

    fireEvent.click(screen.getByLabelText('选择模型'))
    expect(screen.getByRole('listbox', { name: '选择模型' })).toBeTruthy()
    fireEvent.click(screen.getByRole('option', { name: 'gpt · 渠道1 · 官网版' }))
    expect(onChange).toHaveBeenCalledWith('channel-1::gpt')
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('does not render model options when locked', () => {
    const onLocked = vi.fn()
    render(<SelectMenu ariaLabel="选择模型" value="" options={options} onChange={vi.fn()} locked onLocked={onLocked} />)

    fireEvent.click(screen.getByLabelText('选择模型'))
    expect(onLocked).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(screen.queryByRole('option')).toBeNull()
    expect(screen.queryByText('gpt · 渠道1 · 官网版')).toBeNull()
  })
})
