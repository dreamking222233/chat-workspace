// @vitest-environment jsdom

import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import LandingPage from './LandingPage'

describe('LandingPage content', () => {
  afterEach(cleanup)

  it('keeps real capability copy without fictional workspace records', () => {
    const { container } = render(<LandingPage user={null} onNavigate={vi.fn()} onLogout={vi.fn()} />)
    const navigation = screen.getByRole('navigation', { name: '页面导航' })

    expect(within(navigation).getByRole('button', { name: '能力' })).toBeTruthy()
    expect(within(navigation).getByRole('button', { name: '使用流程' })).toBeTruthy()
    expect(within(navigation).queryByRole('button', { name: '工作区' })).toBeNull()
    expect(container.querySelector('.landing-preview')).toBeNull()
    expect(screen.queryByText('品牌视觉')).toBeNull()
    expect(screen.queryByText('产品调研')).toBeNull()
    expect(screen.queryByText('帮我把这周的项目进展整理成可对外同步的摘要。')).toBeNull()
    expect(screen.getByText('自然对话')).toBeTruthy()
    expect(screen.getAllByText('图片生成', { selector: 'h3' })).toHaveLength(2)
  })
})
