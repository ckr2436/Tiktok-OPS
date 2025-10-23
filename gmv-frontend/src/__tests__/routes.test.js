import { describe, it, expect } from './testUtils.js'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

describe('tenant routing definitions', () => {
  it('ensures dashboard is the default index and data overview route is unique', () => {
    const __filename = fileURLToPath(import.meta.url)
    const __dirname = path.dirname(__filename)
    const source = readFileSync(path.join(__dirname, '../routes/index.jsx'), 'utf-8')

    const overviewMatches = source.match(/tenant\/data-overview/g) || []
    expect(overviewMatches.length).toBe(1)

    const hasDashboardIndex = /\{\s*index:\s*true,\s*element:\s*<Dashboard\s*\/?>/.test(source)
    expect(hasDashboardIndex).toBe(true)

    const overviewSection = /path:\s*'tenant\/data-overview'[\s\S]*?element:\s*<TenantDataOverview\s*\/>/.test(source)
    expect(overviewSection).toBe(true)

    const duplicateDashboardPath = source.match(/path:\s*'dashboard'/g) || []
    expect(duplicateDashboardPath.length).toBe(0)
  })
})
