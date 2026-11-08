import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { parse, compileScript } from '@vue/compiler-sfc'
import * as vue from 'vue'
import postcss from 'postcss'

const source = readFileSync(new URL('../src/components/Step5Interaction.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'interaction-personas-test' })

test('persona menu has bounded width inside the wrapping toolbar', () => {
  const rules = new Map()
  postcss.parse(descriptor.styles[0].content).walkRules(rule => {
    rules.set(rule.selector, Object.fromEntries(rule.nodes.filter(n => n.type === 'decl').map(n => [n.prop, n.value])))
  })
  assert.equal(rules.get('.dropdown-menu').width, '320px')
  assert.equal(rules.get('.dropdown-menu')['max-width'], '100%')
  assert.equal(rules.get('.dropdown-menu').right, '0')
  assert.equal(rules.get('.agent-dropdown').position, 'static')
  assert.equal(rules.get('.action-bar-tabs').position, 'relative')
  assert.equal(rules.get('.action-bar-tabs')['flex-wrap'], 'wrap')
  assert.equal(rules.get('.action-bar')['flex-wrap'], 'wrap')
})

test('selecting a persona sends its own interview, not a simulation restart', async () => {
  const calls = []
  const context = vm.createContext({ console, document: { addEventListener() {}, removeEventListener() {} } })
  const modules = {
    vue: { ...vue, onMounted() {}, onUnmounted() {}, watch() {} },
    'vue-i18n': { useI18n: () => ({ t: key => key }) },
    '../api/report': { chatWithReport: async () => ({}), getReport: async () => ({}), getAgentLog: async () => ({}) },
    '../api/simulation': {
      getSimulationProfilesRealtime: async () => ({}),
      interviewAgents: async params => {
        calls.push(params)
        return { success: true, data: { result: { results: { reddit_3: { response: 'Recorded persona response' } } } } }
      },
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
  const state = module.namespace.default.setup({ simulationId: 'sim-existing', reportId: 'report-existing' }, { expose() {}, emit() {} })
  const persona = { username: 'owner', profession: 'A very long professional description '.repeat(40) }
  state.toggleAgentDropdown()
  assert.equal(state.showAgentDropdown.value, true)
  state.selectAgent(persona, 3)
  assert.equal(state.showAgentDropdown.value, false)
  assert.equal(state.selectedAgent.value.username, 'owner')
  await state.sendToAgent('What happened during the simulation?')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].simulation_id, 'sim-existing')
  assert.equal(calls[0].interviews[0].agent_id, 3)
  assert.equal(state.chatHistory.value.at(-1).content, 'Recorded persona response')
})
