import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { parse, compileScript } from '@vue/compiler-sfc'
import * as vue from 'vue'

const source = readFileSync(new URL('../src/components/Step3Simulation.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'simulation-start-test' })

async function monitor({ statuses, startError, startData, details } = {}) {
  const starts = []
  const emits = []
  const intervals = new Map()
  let timer = 0
  let mounted
  let unmounted
  let reads = 0
  let reportCalls = 0
  const context = vm.createContext({
    console,
    setInterval: (callback, delay) => {
      intervals.set(++timer, { callback, delay })
      return timer
    },
    clearInterval: id => intervals.delete(id),
  })
  const statusQueue = [...(statuses || ['idle'])]
  const modules = {
    vue: {
      ...vue,
      onMounted: callback => { mounted = callback },
      onUnmounted: callback => { unmounted = callback },
    },
    'vue-router': { useRouter: () => ({ push() {} }) },
    'vue-i18n': { useI18n: () => ({ t: (key, values) => key + (values ? JSON.stringify(values) : '') }) },
    '../api/report': { generateReport: async () => { reportCalls++; return { success: false } } },
    '../api/simulation': {
      getRunStatus: async () => {
        reads++
        const value = statusQueue.length > 1 ? statusQueue.shift() : statusQueue[0]
        if (value instanceof Error) throw value
        if (typeof value === 'function') return value()
        return {
          success: true,
          data: {
            runner_status: value,
            current_round: value === 'idle' ? 0 : 40,
            total_rounds: 40,
            twitter_current_round: 40,
            reddit_current_round: 40,
            twitter_actions_count: 210,
            reddit_actions_count: 245,
            error: value === 'failed' ? 'ingestion incomplete' : null,
          },
        }
      },
      getRunStatusDetail: async () => ({
        success: true,
        data: { all_actions: details || [{ id: 'action-1', platform: 'twitter', action_type: 'CREATE_POST' }] },
      }),
      startSimulation: async params => {
        starts.push(params)
        if (startError) throw startError
        return { success: true, data: startData || { runner_status: 'running', process_pid: 123 } }
      },
      stopSimulation: async () => ({ success: true }),
    },
  }
  const module = new vm.SourceTextModule(compiled.content, { context })
  await module.link(specifier => {
    const exports = modules[specifier]
    assert.ok(exports, `Unexpected import: ${specifier}`)
    return new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value)
    }, { context })
  })
  await module.evaluate()
  const props = { simulationId: 'sim-existing', maxRounds: 40, minutesPerRound: 60, systemLogs: [] }
  const state = module.namespace.default.setup(props, {
    expose() {},
    emit: (...args) => emits.push(args),
  })
  return {
    state, starts, emits, intervals,
    mount: () => mounted(),
    unmount: () => unmounted(),
    get reads() { return reads },
    get reportCalls() { return reportCalls },
  }
}

for (const status of ['starting', 'running', 'paused', 'stopping', 'completed', 'stopped', 'failed']) {
  test(`opening a ${status} simulation restores it without a start POST`, async () => {
    const view = await monitor({ statuses: [status] })
    await view.mount()
    assert.equal(view.starts.length, 0)
    assert.equal(view.state.runStatus.value.runner_status, status)
    assert.equal(view.state.runStatus.value.current_round, 40)
    assert.equal(view.state.allActions.value.length, 1)
    const terminal = ['completed', 'stopped', 'failed'].includes(status)
    assert.equal(view.intervals.size, terminal ? 0 : 2)
    assert.equal(view.state.canGenerateReport.value, ['completed', 'stopped'].includes(status))
    if (status === 'failed') {
      await view.state.handleNextStep()
      assert.equal(view.reportCalls, 0)
      assert.equal(view.emits.at(-1)[1], 'error')
    }
    view.unmount()
    assert.equal(view.intervals.size, 0)
  })
}

test('an idle simulation starts once without forcing a restart', async () => {
  const view = await monitor()
  await view.mount()
  assert.equal(view.starts.length, 1)
  assert.equal(view.starts[0].force, false)
  assert.equal(view.starts[0].max_rounds, 40)
  assert.equal(view.starts[0].enable_graph_memory_update, true)
  assert.equal(view.state.runStatus.value.runner_status, 'running')
  assert.equal(view.intervals.size, 2)
  view.unmount()
})

for (const status of ['running', 'completed']) {
  test(`a timed-out start is reconciled as ${status}, never replayed`, async () => {
    const view = await monitor({ statuses: ['idle', status], startError: new Error('timeout of 300000ms exceeded') })
    await view.mount()
    assert.equal(view.starts.length, 1)
    assert.equal(view.reads, 2)
    assert.equal(view.state.runStatus.value.runner_status, status)
    assert.equal(view.state.startError.value, null)
    assert.ok(!view.emits.some(event => event[0] === 'update-status' && event[1] === 'error'))
    view.unmount()
  })
}

test('failed or unknown status reads cannot start or clear a simulation', async () => {
  for (const value of [new Error('network unavailable'), 'unrecognized', undefined]) {
    const view = await monitor({ statuses: [value] })
    await view.mount()
    assert.equal(view.starts.length, 0)
    assert.equal(view.intervals.size, 0)
    assert.ok(view.state.startError.value)
    assert.equal(view.emits.at(-1)[1], 'error')
  }
})

test('rejected starts stay failed when the server is still idle', async () => {
  const view = await monitor({ statuses: ['idle'], startError: new Error('start rejected') })
  await view.mount()
  assert.equal(view.starts.length, 1)
  assert.equal(view.state.startError.value, 'start rejected')
  assert.equal(view.intervals.size, 0)
})

test('stopping progress reaches a terminal state and loads the final actions', async () => {
  const actions = [{ id: 'first', platform: 'twitter' }]
  const view = await monitor({ statuses: ['stopping', 'completed'], details: actions })
  await view.mount()
  assert.equal(view.state.canGenerateReport.value, false)
  actions.push({ id: 'last', platform: 'reddit' })
  await view.state.fetchRunStatus()
  assert.equal(view.state.canGenerateReport.value, true)
  assert.equal(view.intervals.size, 0)
  assert.equal(view.state.allActions.value.length, 2)
  assert.equal(view.starts.length, 0)
})

test('leaving during the initial status read never starts a simulation', async () => {
  let resolveStatus
  const view = await monitor({ statuses: [() => new Promise(resolve => { resolveStatus = resolve })] })
  const loading = view.mount()
  view.unmount()
  resolveStatus({ success: true, data: { runner_status: 'idle' } })
  await loading
  assert.equal(view.starts.length, 0)
  assert.equal(view.intervals.size, 0)
})
