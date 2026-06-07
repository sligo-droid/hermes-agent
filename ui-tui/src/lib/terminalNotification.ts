const ESC = '\x1b'
const BEL = '\x07'
const ST = `${ESC}\\`

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

export const writeCompletionNotification = (
  stdout: Pick<NodeJS.WriteStream, 'isTTY' | 'write'> | undefined,
  enabled: boolean,
  env: NodeJS.ProcessEnv = process.env
): boolean => {
  if (!enabled || !stdout?.isTTY) {
    return false
  }

  stdout.write(wrapForMultiplexer(osc777Notify(), env))

  return true
}
