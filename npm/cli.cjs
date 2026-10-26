#!/usr/bin/env node
'use strict'

const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')
const net = require('node:net')
const { spawnSync } = require('node:child_process')

const HELP = `Usage: hyadex-server install [--port PORT] [--bind IP] [--non-interactive] [--dry-run]
       hyadex-server recover
       hyadex-server --version

install: fresh default user service only; existing installations use the source installer.
recover: on the server computer, recover an interrupted pre-activation migration.
No installation hooks run when this npm package is installed.
`
const ROOT_SELECTORS = ['HYADEX_SERVER_INSTALL_DIR', 'HYADEX_SERVER_CONFIG_DIR', 'HYADEX_SERVER_STATE_DIR', 'HYADEX_STATE_DIR', 'ZENITHBOT_AGENT_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME']

function parse(argv) {
  const [command = 'help', ...args] = argv
  if (['help', '--help', '-h', '--version'].includes(command)) {
    if (args.length) throw new Error('Unexpected arguments.')
    return { command }
  }
  const values = command === 'install' ? ['--port', '--bind'] : []
  if (!['install', 'recover'].includes(command)) throw new Error('Use install, recover, or --help. Managed update commands have been removed.')
  const flags = command === 'install' ? ['--non-interactive', '--dry-run'] : []
  const options = {}
  for (let index = 0; index < args.length; index++) {
    const name = args[index]
    if (Object.hasOwn(options, name)) throw new Error(`Duplicate option: ${name}`)
    if (flags.includes(name)) options[name] = true
    else if (values.includes(name)) {
      const value = args[++index]
      if (!value || value.startsWith('--') || /[\x00-\x1f\x7f]/.test(value)) throw new Error(`Missing or invalid value for ${name}`)
      options[name] = value
    } else throw new Error(`Unsupported option: ${name}`)
  }
  if (options['--port'] && (!/^[1-9][0-9]{0,4}$/.test(options['--port']) || Number(options['--port']) > 65535)) throw new Error('Port must be an integer from 1 to 65535.')
  if (options['--bind'] && !net.isIP(options['--bind'])) throw new Error('Bind address must be a literal IP address.')
  return { command, options }
}

function readRegular(filename, maxBytes) {
  const descriptor = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0))
  try {
    const info = fs.fstatSync(descriptor)
    if (!info.isFile() || info.size > maxBytes || info.nlink !== 1) throw new Error('Expected a bounded regular file.')
    const bytes = fs.readFileSync(descriptor)
    if (bytes.length > maxBytes) throw new Error('File exceeded its size limit.')
    return bytes
  } finally { fs.closeSync(descriptor) }
}

function ensureFreshInstall(context) {
  if (context.uid === 0) throw new Error('Run as the user who will own the server, without sudo.')
  if (!['darwin', 'linux'].includes(context.platform)) throw new Error('Server installation requires Linux or Apple silicon macOS.')
  for (const name of ROOT_SELECTORS) if (context.env[name]) throw new Error(`Custom installation selector ${name} is unsupported by the fresh-install CLI. Use the source installer after stopping the server.`)
  const existingInstall = (message = 'An existing server installation or state was found. Stop the server and use the source installer; no installer was started.') =>
    Object.assign(new Error(message), { code: 'HYADEX_EXISTING_INSTALLATION' })
  const safeDirectory = info => info.isDirectory() && !info.isSymbolicLink() && info.uid === context.uid && (info.mode & 0o022) === 0 && (info.mode & 0o500) === 0o500
  const allowEmptyScaffold = (relative, child) => {
    const root = path.join(context.home, relative)
    let rootInfo
    try { rootInfo = fs.lstatSync(root) } catch (error) { if (error.code !== 'ENOENT') throw error }
    if (!rootInfo) return
    // Failed bootstrap or preactivation can leave these exact empty directories.
    // Never delete them or admit files, credentials, histories, links or locks.
    // install.sh repeats this check under exclusive installation ownership.
    if (!safeDirectory(rootInfo)) throw existingInstall()
    const entries = fs.readdirSync(root)
    if (entries.length > 1 || (entries.length === 1 && entries[0] !== child)) throw existingInstall()
    if (entries.length === 1) {
      const nested = path.join(root, child)
      if (!safeDirectory(fs.lstatSync(nested)) || fs.readdirSync(nested).length !== 0) throw existingInstall()
    }
  }
  allowEmptyScaffold('.local/share/hyadex-server', 'releases')
  allowEmptyScaffold('.config/hyadex-server')
  allowEmptyScaffold('.hyadex', 'admin')
  const targets = [
    '.zenithbot-agent',
    '.config/systemd/user/hyadex-server.service', '.config/systemd/user/zenithbot-agent.service',
    'Library/LaunchAgents/com.hyadex.server.plist',
  ]
  for (const relative of targets) {
    try {
      fs.lstatSync(path.join(context.home, relative))
      throw existingInstall()
    } catch (error) { if (error.code !== 'ENOENT') throw error }
  }
  if (context.platform === 'darwin') {
    const check = context.spawn('/bin/launchctl', ['print', `gui/${context.uid}/com.hyadex.server`], { encoding: 'utf8', timeout: 10000 })
    if (check.status === 0) throw existingInstall('An HyadexServer service already exists. Stop the server and use the source installer.')
    if (check.error || !/could not find service|service.*not found/i.test(`${check.stdout || ''} ${check.stderr || ''}`)) throw new Error('Could not verify the user service is absent. No installer was started.')
  } else {
    for (const unit of ['hyadex-server.service', 'zenithbot-agent.service']) {
      const check = context.spawn('systemctl', ['--user', 'show', unit, '--property=LoadState', '--value'], { encoding: 'utf8', timeout: 10000 })
      if (!check.error && check.status === 0 && String(check.stdout).trim() && String(check.stdout).trim() !== 'not-found') {
        throw existingInstall('An existing user service was found. Stop the server and use the source installer.')
      }
      if (check.error || String(check.stdout).trim() !== 'not-found') throw new Error('An existing user service was found or its absence could not be verified. Stop the server and use the source installer.')
    }
  }
}

function recoveryContext(context) {
  if (context.uid === 0) throw new Error('Run as the user who owns the server, without sudo.')
  if (!['darwin', 'linux'].includes(context.platform)) throw new Error('Server recovery requires Linux or Apple silicon macOS.')
  for (const name of ROOT_SELECTORS) if (context.env[name]) throw new Error(`Custom installation selector ${name} is unsupported by recovery; no installer was started.`)
  const inspect = filename => {
    try { return fs.lstatSync(filename) } catch (error) { if (error.code !== 'ENOENT') throw error; return null }
  }
  const directory = (filename, { optional = false, privateDirectory = false } = {}) => {
    const info = inspect(filename)
    if (!info && optional) return null
    if (!info || !info.isDirectory() || info.isSymbolicLink() || info.uid !== context.uid ||
        (info.mode & (privateDirectory ? 0o077 : 0o022)) !== 0 || (info.mode & 0o500) !== 0o500) {
      throw new Error('Recovery requires owned, unlinked, safely permissioned default installation directories.')
    }
    return info
  }
  const readPrivate = (filename, limit) => {
    const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK)
    try {
      const info = fs.fstatSync(fd)
      if (!info.isFile() || info.uid !== context.uid || (info.mode & 0o077) !== 0 || info.nlink !== 1 || info.size > limit) {
        throw new Error('Recovery requires a bounded, private, current-user-owned regular control file.')
      }
      const bytes = fs.readFileSync(fd)
      if (bytes.length > limit) throw new Error('Recovery control file exceeded its size limit.')
      return bytes
    } finally { fs.closeSync(fd) }
  }
  directory(context.home)
  let root = context.home
  for (const component of ['.local', 'share', 'hyadex-server']) {
    root = path.join(root, component)
    if (!directory(root, { optional: true })) return null
  }
  const rootInfo = directory(root)
  const journalDirectory = path.join(root, '.activation-transaction')
  if (!directory(journalDirectory, { optional: true, privateDirectory: true })) return null
  const journal = JSON.parse(readPrivate(path.join(journalDirectory, 'manifest.json'), 256 * 1024))
  const origin = ['rolling-back', 'rolled-back', 'rollback-healthy'].includes(journal.phase) ? journal.rollback_from : journal.phase
  const config = path.join(context.home, '.config/hyadex-server')
  const state = path.join(context.home, '.hyadex')
  const service = context.platform === 'darwin' ? path.join(context.home, 'Library/LaunchAgents/com.hyadex.server.plist') : path.join(context.home, '.config/systemd/user/hyadex-server.service')
  if (journal.format !== 3 || !['prepared', 'guarded'].includes(origin) || journal.intent !== 'server-update' ||
      !/^activation-[0-9a-f]{24}$/.test(journal.transaction_id) ||
      !/^\d+\.\d+\.\d+(?:-beta\.[1-9]\d*)?$/.test(journal.release_version) ||
      journal.release_dir !== path.join(root, 'releases', journal.release_version) ||
      journal.env_path !== path.join(config, 'env') || journal.service_path !== service ||
      journal.service_state !== 'running' || journal.execution?.gateway_state !== 'absent' ||
      journal.execution?.runtime_dir !== path.join(state, 'execution') ||
      !Number.isSafeInteger(journal.execution?.api_contract) || journal.execution.api_contract < 1 ||
      journal.hub?.kind !== 'server-update' || journal.hub?.data_dir !== path.join(state, 'team-hub')) {
    throw new Error('This journal is not a supported interrupted pre-activation migration; no installer was started.')
  }
  directory(state, { privateDirectory: true })
  const identity = readPrivate(path.join(state, 'server-identity'), 1024).toString('utf8').trim()
  if (!/^[A-Za-z0-9_.:-]{8,240}$/.test(identity) || journal.hub.host_identity !== identity) {
    throw new Error('The pending migration belongs to a different server identity; no installer was started.')
  }
  // The installer validates complete native configuration, links, ownership and
  // authenticated incumbent health under its lock. These checks only select
  // the exact existing transaction; they never grant recovery authority.
  const ownerParent = path.join(root, '.activation-recovery')
  if (directory(ownerParent, { optional: true, privateDirectory: true }) && inspect(path.join(ownerParent, journal.transaction_id))) {
    throw new Error('This activation already has a recovery owner; no competing recovery was started.')
  }
  return { root, rootInfo, journalDirectory, journal, identity }
}

async function run(argv, overrides = {}) {
  const context = { env: process.env, home: os.homedir(), uid: process.getuid?.(), platform: process.platform, spawn: spawnSync, packageRoot: path.resolve(__dirname, '..'), print: text => process.stdout.write(`${text}\n`), ...overrides }
  const { command, options } = parse(argv)
  if (['help', '--help', '-h'].includes(command)) { context.print(HELP); return 0 }
  const metadata = JSON.parse(readRegular(path.join(context.packageRoot, 'package.json'), 64000))
  if (command === '--version') { context.print(metadata.version); return 0 }
  const payload = path.join(context.packageRoot, 'server')
  const payloadVersion = readRegular(path.join(payload, 'VERSION'), 200).toString('utf8').trim()
  if (payloadVersion !== metadata.version) throw new Error('Package and server payload versions do not match.')
  if (command === 'recover') {
    const recovery = recoveryContext(context)
    if (!recovery) { context.print('No unfinished default server migration was found. No changes were made.'); return 0 }
    const installer = path.join(payload, 'install.sh')
    readRegular(installer, 2 * 1024 * 1024)
    const args = [installer, '--recover-only', '--recover-unarmed-only', '--non-interactive', '--execution-mode', 'split',
      '--expected-activation-id', recovery.journal.transaction_id, '--release-version', recovery.journal.release_version,
      '--expected-api-contract', String(recovery.journal.execution.api_contract), '--expected-server-identity', recovery.identity]
    const env = { HOME: context.home }
    for (const key of ['PATH', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR', 'LANG', 'LC_ALL', 'TERM', 'TERMINFO', 'SSL_CERT_FILE', 'SSL_CERT_DIR']) {
      if (context.env[key]) env[key] = context.env[key]
    }
    const result = context.spawn('/bin/bash', args, { stdio: 'inherit', env })
    if (result.error) throw new Error('Could not launch the bundled recovery installer.')
    if (![0, 75].includes(result.status)) return Number.isInteger(result.status) ? result.status : 1
    const rootAfter = fs.lstatSync(recovery.root)
    if (!rootAfter.isDirectory() || rootAfter.isSymbolicLink() || rootAfter.dev !== recovery.rootInfo.dev || rootAfter.ino !== recovery.rootInfo.ino) {
      throw new Error('Installation ownership changed during recovery; verify the server before retrying.')
    }
    try { fs.lstatSync(recovery.journalDirectory); throw new Error('The activation journal remains; recovery is not complete.') } catch (error) { if (error.code !== 'ENOENT') throw error }
    context.print('The interrupted migration was recovered. Check the recovered server before using the source installer; this command has not upgraded it.')
    return 0
  }
  if (command === 'install') {
    ensureFreshInstall(context)
    const installer = path.join(payload, 'install.sh')
    readRegular(installer, 2 * 1024 * 1024)
    const args = [installer, '--fresh-install-only', '--release-version', metadata.version]
    for (const name of ['--port', '--bind']) if (options[name]) args.push(name, options[name])
    if (options['--non-interactive']) args.push('--non-interactive')
    if (options['--dry-run']) { context.print(JSON.stringify({ command: '/bin/bash', args, executed: false })); return 0 }
    const result = context.spawn('/bin/bash', args, { stdio: 'inherit', env: context.env })
    if (result.error) throw new Error('Could not launch the bundled installer.')
    return Number.isInteger(result.status) ? result.status : 1
  }

}

if (require.main === module) run(process.argv.slice(2)).then(code => { process.exitCode = code }).catch(error => { process.stderr.write(`hyadex-server: ${error.message}\n`); process.exitCode = 1 })
module.exports = { parse, readRegular, ensureFreshInstall, recoveryContext, run }
