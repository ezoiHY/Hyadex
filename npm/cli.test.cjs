'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawnSync } = require('node:child_process')
const { parse, run } = require('./cli.cjs')

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hyadex-npm-cli-'))
  t.after(() => fs.rmSync(root, { recursive: true, force: true }))
  const packageRoot = path.join(root, 'package')
  const home = path.join(root, 'home')
  fs.mkdirSync(path.join(packageRoot, 'server'), { recursive: true })
  fs.mkdirSync(home)
  fs.writeFileSync(path.join(packageRoot, 'package.json'), JSON.stringify({ version: '1.2.3-beta.4' }))
  fs.writeFileSync(path.join(packageRoot, 'server/VERSION'), '1.2.3-beta.4\n')
  fs.writeFileSync(path.join(packageRoot, 'server/install.sh'), '#!/bin/bash\nexit 73\n')
  const output = []
  const calls = []
  const context = { packageRoot, home, env: {}, platform: 'linux', uid: process.getuid(), print: text => output.push(text), spawn: (...args) => { calls.push(args); return { status: 0, stdout: 'not-found\n' } } }
  return { root, packageRoot, home, context, output, calls }
}

test('legacy recovery rejects before reading credentials or launching an installer', async () => {
  assert.throws(() => parse(['recover']), /removed/)
  await assert.rejects(run(['recover'], { packageRoot: '/not-read', spawn: () => { throw new Error('must not execute') } }), /removed/)
})

test('rejects arbitrary installer flags and invalid values before delegation', () => {
  for (const args of [['install', '--release-version', '9.9.9'], ['install', '--instance', 'other'], ['install', '--port', '0'], ['install', '--port', '65536'], ['install', '--bind', 'anything;command'], ['install', '--port', '7870', '--port', '7851']]) assert.throws(() => parse(args))
  assert.throws(() => parse(['update']), /removed/)
})

test('fresh install only delegates validated arguments to the bundled installer', async t => {
  const f = fixture(t)
  await run(['install', '--port', '7851', '--bind', '127.0.0.1', '--non-interactive'], { ...f.context, uid: 501 })
  const install = f.calls.at(-1)
  assert.equal(install[0], '/bin/bash')
  assert.deepEqual(install[1], [path.join(f.packageRoot, 'server/install.sh'), '--fresh-install-only', '--port', '7851', '--bind', '127.0.0.1', '--non-interactive'])
  assert.equal(install[2].stdio, 'inherit')
})

test('fresh guard refuses an existing install or state without spawning anything', async t => {
  const f = fixture(t)
  fs.mkdirSync(path.join(f.home, '.hyadex'))
  fs.writeFileSync(path.join(f.home, '.hyadex/server-identity'), 'existing-server')
  await assert.rejects(run(['install'], f.context), /existing server installation or state/)
  assert.equal(f.calls.length, 0)
})

test('fresh install retries real bootstrap and preactivation subprocess failures without deleting empty scaffolding', async t => {
  const f = fixture(t)
  const attempt = path.join(f.root, 'attempt')
  const preactivationAttempt = path.join(f.root, 'preactivation-attempt')
  const completed = path.join(f.root, 'completed')
  fs.writeFileSync(path.join(f.packageRoot, 'server/install.sh'), `#!/bin/bash
set -eu
mkdir -p "$QA_FRESH_HOME/.local/share/hyadex-server/releases"
if [[ ! -e "$QA_INSTALL_ATTEMPT" ]]; then
  touch "$QA_INSTALL_ATTEMPT"
  exit 73
fi
mkdir -p "$QA_FRESH_HOME/.config/hyadex-server" "$QA_FRESH_HOME/.hyadex/admin"
chmod 700 "$QA_FRESH_HOME/.config/hyadex-server" "$QA_FRESH_HOME/.hyadex" "$QA_FRESH_HOME/.hyadex/admin"
if [[ ! -e "$QA_PREACTIVATION_ATTEMPT" ]]; then
  touch "$QA_PREACTIVATION_ATTEMPT"
  exit 74
fi
touch "$QA_INSTALL_COMPLETED"
`)
  let installs = 0
  const context = {
    ...f.context,
    env: { ...process.env, QA_FRESH_HOME: f.home, QA_INSTALL_ATTEMPT: attempt, QA_PREACTIVATION_ATTEMPT: preactivationAttempt, QA_INSTALL_COMPLETED: completed },
    spawn: (command, args, options) => {
      if (command !== '/bin/bash') return { status: 0, stdout: 'not-found\n' }
      installs++
      return spawnSync(command, args, options)
    },
  }
  for (const key of ['HYADEX_SERVER_INSTALL_DIR', 'HYADEX_SERVER_CONFIG_DIR', 'HYADEX_SERVER_STATE_DIR', 'HYADEX_STATE_DIR', 'ZENITHBOT_AGENT_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME']) delete context.env[key]
  assert.equal(await run(['install', '--non-interactive'], context), 73)
  const installRoot = path.join(f.home, '.local/share/hyadex-server')
  assert.deepEqual(fs.readdirSync(installRoot), ['releases'])
  assert.deepEqual(fs.readdirSync(path.join(installRoot, 'releases')), [])
  assert.equal(fs.existsSync(path.join(f.home, '.hyadex')), false)
  assert.equal(fs.existsSync(path.join(f.home, '.config/hyadex-server')), false)
  assert.equal(await run(['install', '--non-interactive'], context), 74)
  const config = path.join(f.home, '.config/hyadex-server'), state = path.join(f.home, '.hyadex'), admin = path.join(state, 'admin')
  assert.deepEqual(fs.readdirSync(config), [])
  assert.deepEqual(fs.readdirSync(state), ['admin'])
  assert.deepEqual(fs.readdirSync(admin), [])
  const identities = [installRoot, config, state, admin].map(name => fs.statSync(name).ino)
  assert.equal(await run(['install', '--non-interactive'], context), 0)
  assert.equal(installs, 3)
  assert.deepEqual([installRoot, config, state, admin].map(name => fs.statSync(name).ino), identities)
  assert.equal(fs.existsSync(completed), true)
  fs.mkdirSync(path.join(installRoot, '.install-lock'))
  await assert.rejects(run(['install'], context), /existing server installation or state/)
  assert.equal(installs, 3)
})

test('empty scaffolding admission still refuses files, links, releases, unsafe ownership and state', async t => {
  const cases = [
    (root) => fs.writeFileSync(root, 'existing'),
    (root, f) => fs.symlinkSync(f.home, root),
    (root) => { fs.mkdirSync(root); fs.writeFileSync(path.join(root, 'releases'), 'existing') },
    (root, f) => { fs.mkdirSync(root); fs.symlinkSync(f.home, path.join(root, 'releases')) },
    (root) => fs.mkdirSync(path.join(root, 'releases/older-version'), { recursive: true }),
    (root) => fs.mkdirSync(path.join(root, 'releases/.stage-other'), { recursive: true }),
    (root) => { fs.mkdirSync(root); fs.writeFileSync(path.join(root, '.hidden'), 'existing') },
    (root) => { fs.mkdirSync(root); fs.chmodSync(root, 0o777) },
    (root) => { fs.mkdirSync(path.join(root, 'releases'), { recursive: true }); fs.chmodSync(path.join(root, 'releases'), 0o777) },
    (root, f) => { fs.mkdirSync(root); f.context.uid++ },
    (root, f) => { fs.mkdirSync(root); fs.mkdirSync(path.join(f.home, '.hyadex')); fs.writeFileSync(path.join(f.home, '.hyadex/server-identity'), 'existing') },
    (root, f) => { fs.mkdirSync(root); fs.mkdirSync(path.join(f.home, '.config/hyadex-server'), { recursive: true }); fs.writeFileSync(path.join(f.home, '.config/hyadex-server/env'), 'existing') },
  ]
  for (const setup of cases) {
    const f = fixture(t)
    const root = path.join(f.home, '.local/share/hyadex-server')
    fs.mkdirSync(path.dirname(root), { recursive: true })
    setup(root, f)
    await assert.rejects(run(['install'], f.context), /existing server installation or state/)
    assert.equal(f.calls.length, 0)
    assert.equal(fs.lstatSync(root).isSymbolicLink() || fs.existsSync(root), true)
  }
})

test('empty config and state admission rejects credentials, histories, unknown directories, locks and unsafe entries', async t => {
  const cases = [
    ['.config/hyadex-server', (root) => fs.writeFileSync(root, 'existing')],
    ['.config/hyadex-server', (root, f) => fs.symlinkSync(f.home, root)],
    ['.config/hyadex-server', (root) => { fs.mkdirSync(root); fs.writeFileSync(path.join(root, 'env'), 'existing-token') }],
    ['.config/hyadex-server', (root) => fs.mkdirSync(path.join(root, 'admin'), { recursive: true })],
    ['.hyadex', (root) => fs.writeFileSync(root, 'existing')],
    ['.hyadex', (root, f) => fs.symlinkSync(f.home, root)],
    ['.hyadex', (root) => { fs.mkdirSync(root); fs.writeFileSync(path.join(root, 'server-identity'), 'existing') }],
    ['.hyadex', (root) => fs.mkdirSync(path.join(root, 'chats'), { recursive: true })],
    ['.hyadex', (root) => fs.mkdirSync(path.join(root, '.lock'), { recursive: true })],
    ['.hyadex', (root) => { fs.mkdirSync(root); fs.writeFileSync(path.join(root, 'admin'), 'existing') }],
    ['.hyadex', (root, f) => { fs.mkdirSync(root); fs.symlinkSync(f.home, path.join(root, 'admin')) }],
    ['.hyadex', (root) => { fs.mkdirSync(path.join(root, 'admin'), { recursive: true }); fs.writeFileSync(path.join(root, 'admin', 'pending-update'), 'existing') }],
    ['.hyadex', (root) => fs.mkdirSync(path.join(root, 'admin', '.lock'), { recursive: true })],
    ['.zenithbot-agent', (root) => fs.mkdirSync(root)],
  ]
  for (const relative of ['.config/hyadex-server', '.hyadex', '.hyadex/admin']) {
    for (const mode of [0o770, 0o707, 0o300]) cases.push([relative, root => { fs.mkdirSync(root, { recursive: true }); fs.chmodSync(root, mode) }])
    cases.push([relative, (root, f) => { fs.mkdirSync(root, { recursive: true }); f.context.uid++ }])
  }
  for (const [relative, setup] of cases) {
    const f = fixture(t), root = path.join(f.home, relative)
    fs.mkdirSync(path.dirname(root), { recursive: true })
    setup(root, f)
    const before = fs.lstatSync(root)
    await assert.rejects(run(['install'], f.context), /existing server installation or state/)
    assert.equal(f.calls.length, 0)
    assert.equal(fs.lstatSync(root).ino, before.ino)
    if (before.isDirectory()) fs.chmodSync(root, 0o700)
  }
})

test('fresh guard refuses custom roots, root user, loaded service and inspection failure', async t => {
  const f = fixture(t)
  await assert.rejects(run(['install'], { ...f.context, uid: 501, env: { HYADEX_SERVER_INSTALL_DIR: '/custom' } }), /Custom installation/)
  await assert.rejects(run(['install'], { ...f.context, uid: 0 }), /without sudo/)
  for (const stdout of ['loaded', '']) await assert.rejects(run(['install'], { ...f.context, uid: 501, spawn: () => ({ status: 0, stdout }) }), /existing user service|absence could not/)
})

test('dry run checks absence but never starts installer', async t => {
  const f = fixture(t)
  await run(['install', '--dry-run'], { ...f.context, uid: 501 })
  assert.equal(f.calls.some(call => call[0] === '/bin/bash'), false)
  assert.equal(JSON.parse(f.output[0]).executed, false)
})

test('removed update command never reads credentials, contacts a server or starts an installer', async t => {
  const f = fixture(t)
  let requests = 0
  const context = { ...f.context, request: () => { requests++; throw new Error('must not request') } }
  await assert.rejects(run(['update', '--server-url', 'http://127.0.0.1:1',
    '--token-file', '/does-not-exist', '--manifest', '/does-not-exist', '--signature', '/does-not-exist'], context), /removed/)
  assert.equal(requests, 0)
  assert.equal(f.calls.length, 0)
})
