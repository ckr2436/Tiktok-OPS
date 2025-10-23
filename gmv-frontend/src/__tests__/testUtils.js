import { isDeepStrictEqual, inspect } from 'node:util'

const rootSuites = []
let currentSuite = null

function describe(name, fn) {
  const suite = { name, items: [], beforeEach: [], afterEach: [] }
  if (currentSuite) {
    currentSuite.items.push({ type: 'suite', suite })
  } else {
    rootSuites.push(suite)
  }
  const previous = currentSuite
  currentSuite = suite
  try {
    fn()
  } finally {
    currentSuite = previous
  }
}

function ensureSuite() {
  if (!currentSuite) {
    throw new Error('`it` must be used inside a describe block')
  }
}

function it(name, fn) {
  ensureSuite()
  currentSuite.items.push({ type: 'test', name, fn })
}

function beforeEach(fn) {
  ensureSuite()
  currentSuite.beforeEach.push(fn)
}

function afterEach(fn) {
  ensureSuite()
  currentSuite.afterEach.push(fn)
}

function format(value) {
  return inspect(value, { depth: 6 })
}

function expect(received) {
  return {
    toBe(expected) {
      if (!Object.is(received, expected)) {
        throw new Error(`Expected ${format(received)} to be ${format(expected)}`)
      }
    },
    toEqual(expected) {
      if (!isDeepStrictEqual(received, expected)) {
        throw new Error(`Expected ${format(received)} to equal ${format(expected)}`)
      }
    },
    toBeTruthy() {
      if (!received) {
        throw new Error(`Expected ${format(received)} to be truthy`)
      }
    },
    toHaveLength(length) {
      if (!received || typeof received.length !== 'number' || received.length !== length) {
        throw new Error(`Expected value to have length ${length}, got ${format(received?.length)}`)
      }
    },
    toBeGreaterThanOrEqual(expected) {
      if (!(received >= expected)) {
        throw new Error(`Expected ${format(received)} to be >= ${format(expected)}`)
      }
    },
    toHaveBeenCalledTimes(times) {
      if (!received || !received.mock) {
        throw new Error('Expected a mock function')
      }
      const actual = received.mock.calls.length
      if (actual !== times) {
        throw new Error(`Expected mock to be called ${times} times, but was called ${actual} times`)
      }
    },
  }
}

function createMock(impl = () => {}) {
  let handler = impl
  const fn = (...args) => {
    fn.mock.calls.push(args)
    return handler(...args)
  }
  fn.mock = { calls: [] }
  fn.mockImplementation = (next) => {
    handler = typeof next === 'function' ? next : () => next
    return fn
  }
  fn.mockReturnValue = (value) => fn.mockImplementation(() => value)
  return fn
}

const timerState = {
  fake: false,
  currentTime: Date.now(),
  timers: new Map(),
  nextId: 1,
  originals: null,
  lastError: null,
}

function ensureFakeTimers() {
  if (!timerState.fake) {
    throw new Error('Fake timers are not enabled. Call vi.useFakeTimers() first.')
  }
}

function registerTimer(type, fn, delay, args) {
  const id = timerState.nextId++
  const callback = typeof fn === 'function' ? fn : () => fn
  const ms = Number.isFinite(Number(delay)) ? Number(delay) : 0
  timerState.timers.set(id, {
    type,
    callback,
    args,
    delay: ms < 0 ? 0 : ms,
    next: timerState.currentTime + (ms < 0 ? 0 : ms),
  })
  return id
}

function installFakeTimers() {
  if (timerState.fake) return
  timerState.fake = true
  timerState.currentTime = Date.now()
  timerState.timers.clear()
  timerState.nextId = 1
  timerState.lastError = null
  timerState.originals = {
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
    setInterval: globalThis.setInterval,
    clearInterval: globalThis.clearInterval,
    DateNow: Date.now,
  }
  globalThis.setTimeout = (fn, ms = 0, ...args) => registerTimer('timeout', fn, ms, args)
  globalThis.setInterval = (fn, ms = 0, ...args) => registerTimer('interval', fn, ms, args)
  globalThis.clearTimeout = (id) => {
    timerState.timers.delete(id)
  }
  globalThis.clearInterval = globalThis.clearTimeout
  Date.now = () => timerState.currentTime
}

function uninstallFakeTimers() {
  if (!timerState.fake) return
  timerState.fake = false
  timerState.timers.clear()
  if (timerState.originals) {
    globalThis.setTimeout = timerState.originals.setTimeout
    globalThis.clearTimeout = timerState.originals.clearTimeout
    globalThis.setInterval = timerState.originals.setInterval
    globalThis.clearInterval = timerState.originals.clearInterval
    Date.now = timerState.originals.DateNow
  }
  timerState.originals = null
}

const vi = {
  fn: createMock,
  useFakeTimers() {
    installFakeTimers()
  },
  useRealTimers() {
    uninstallFakeTimers()
  },
  setSystemTime(value) {
    const ts = value instanceof Date ? value.getTime() : new Date(value).getTime()
    if (!Number.isFinite(ts)) {
      throw new Error('Invalid time value passed to setSystemTime')
    }
    timerState.currentTime = ts
  },
  advanceTimersByTime(ms) {
    ensureFakeTimers()
    const increment = Number(ms) || 0
    const target = timerState.currentTime + (increment < 0 ? 0 : increment)
    while (true) {
      let nextId = null
      let nextTime = Infinity
      for (const [id, timer] of timerState.timers.entries()) {
        if (timer.next < nextTime) {
          nextTime = timer.next
          nextId = id
        }
      }
      if (nextTime > target) break
      timerState.currentTime = nextTime
      const timer = timerState.timers.get(nextId)
      if (!timer) continue
      try {
        timer.callback(...(timer.args || []))
      } catch (err) {
        timerState.lastError = err
      }
      if (timer.type === 'interval') {
        timer.next = timer.next + (timer.delay || 0)
      } else {
        timerState.timers.delete(nextId)
      }
    }
    timerState.currentTime = target
    if (timerState.lastError) {
      const err = timerState.lastError
      timerState.lastError = null
      throw err
    }
  },
}

async function runSuite(suite, ancestors, results) {
  const chain = ancestors.concat(suite)
  for (const item of suite.items) {
    if (item.type === 'suite') {
      await runSuite(item.suite, chain, results)
      continue
    }
    let error = null
    for (const ancestor of chain) {
      for (const hook of ancestor.beforeEach) {
        await hook()
      }
    }
    try {
      await item.fn()
    } catch (err) {
      error = err
    }
    for (const ancestor of chain.slice().reverse()) {
      for (const hook of ancestor.afterEach) {
        try {
          await hook()
        } catch (err) {
          if (!error) error = err
        }
      }
    }
    results.push({
      name: [...chain.map((s) => s.name), item.name].join(' > '),
      error,
    })
  }
}

async function runSuites() {
  const results = []
  for (const suite of rootSuites) {
    await runSuite(suite, [], results)
  }
  return results
}

function clearSuites() {
  rootSuites.length = 0
  currentSuite = null
}

export { describe, it, beforeEach, afterEach, expect, vi, runSuites, clearSuites }
