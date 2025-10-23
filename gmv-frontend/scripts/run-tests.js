import { readdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { runSuites, clearSuites } from '../src/__tests__/testUtils.js'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const testsRoot = path.join(__dirname, '..', 'src', '__tests__')

async function collectTestFiles(dir) {
  const entries = await readdir(dir, { withFileTypes: true })
  const files = []
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name)
    if (entry.isDirectory()) {
      files.push(...(await collectTestFiles(fullPath)))
    } else if (/\.test\.[jt]sx?$/i.test(entry.name)) {
      files.push(fullPath)
    }
  }
  return files
}

async function main() {
  let files = []
  try {
    files = await collectTestFiles(testsRoot)
  } catch (err) {
    console.error('Failed to read tests directory:', err)
    process.exitCode = 1
    return
  }

  if (files.length === 0) {
    console.log('No tests found.')
    return
  }

  files.sort()
  for (const file of files) {
    await import(pathToFileURL(file).href)
  }

  const results = await runSuites()
  clearSuites()

  let failed = 0
  for (const result of results) {
    if (result.error) {
      failed += 1
      console.error(`✖ ${result.name}`)
      console.error(result.error.stack || result.error.message)
    } else {
      console.log(`✔ ${result.name}`)
    }
  }

  console.log(`\nTest files: ${files.length}`)
  console.log(`Assertions: ${results.length}`)
  if (failed > 0) {
    console.log(`Failed: ${failed}`)
    process.exitCode = 1
  } else {
    console.log('All tests passed')
  }
}

main()
