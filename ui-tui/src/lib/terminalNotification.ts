const ESC = '\x1b'
const BEL = '\x07'
const ST = `${ESC}\\`

// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\x00-\x1f\x7f]/g

export const sanitizeOscField = (value: string): string => value.replace(CONTROL_RE, ' ').replaceAll(';', ',').trim()

export const osc777Notify = (title = 'Hermes', message = 'Response complete'): string => {
  const safeTitle = sanitizeOscField(title) || 'Hermes'
  const safeMessage = sanitizeOscField(message) || 'Response complete'

  return `${ESC}]777;notify;${safeTitle};${safeMessage}${BEL}`
}

export const wrapForMultiplexer = (sequence: string, env: NodeJS.ProcessEnv = process.env): string => {
  if (env.TMUX) {
    return `${ESC}Ptmux;${sequence.replaceAll(ESC, ESC + ESC)}${ST}`
  }

  if (env.STY) {
    return `${ESC}P${sequence}${ST}`
  }

  return sequence
}

export const isUnsafeCompletionNotificationSession = (env: NodeJS.ProcessEnv = process.env): boolean => {
  const termProgram = (env.TERM_PROGRAM ?? '').toLowerCase()

  return Boolean(
    env.TMUX ||
      env.STY ||
      env.CMUX_WORKSPACE_ID ||
      env.CMUX_SURFACE_ID ||
      env.CMUX_TAB_ID ||
      env.CMUX_PANEL_ID ||
      env.CMUX_SOCKET_PATH ||
      env.__CFBundleIdentifier === 'com.cmuxterm.app' ||
      env.SSH_CONNECTION ||
      env.SSH_TTY ||
      termProgram === 'cmux'
  )
}

export const writeCompletionNotification = (
  stdout: Pick<NodeJS.WriteStream, 'isTTY' | 'write'> | undefined,
  enabled: boolean,
  env: NodeJS.ProcessEnv = process.env
): boolean => {
  if (!enabled || !stdout?.isTTY) {
    return false
  }

  // The TUI owns stdout through Ink's frame renderer. Completion notifications
  // are out-of-band OSC writes that fire exactly as the final assistant message
  // enters history. In tmux/screen/cmux/SSH sessions, DCS/OSC handling is not
  // guaranteed to be atomic with the following Ink repaint and can leave the
  // terminal parser/cell grid out of sync, producing scattered final-answer
  // glyphs. Suppress the nice-to-have notification where that risk is known;
  // keep the primary transcript surface safe.
  if (isUnsafeCompletionNotificationSession(env)) {
    return false
  }

  stdout.write(wrapForMultiplexer(osc777Notify(), env))

  return true
}
