#!/usr/bin/env node
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const vitestBin = resolve(__dirname, '../node_modules/.bin/vitest')
const extraArgs = process.argv.slice(2).filter((arg) => arg !== '-r')

const child = spawn(vitestBin, ['run', ...extraArgs], { stdio: 'inherit', shell: process.platform === 'win32' })
child.on('exit', (code) => {
  process.exit(code ?? 0)
})
