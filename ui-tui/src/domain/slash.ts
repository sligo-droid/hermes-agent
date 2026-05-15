/** Appended to `/model` args from the TUI picker for session scope; stripped in `session` slash before `config.set`. */
export const TUI_SESSION_MODEL_FLAG = '--tui-session'

export const looksLikeSlashCommand = (text: string) => /^\/[^\s/]*(?:\s|$)/.test(text)

export const parseSlashCommand = (cmd: string) => {
  const match = cmd.match(/^\/([^\s/]*)(?:([\s\S]*))?$/)
  const name = match?.[1] ?? ''
  const tail = match?.[2] ?? ''
  const arg = tail.replace(/^\s+/, '')

  return { arg, cmd, name: name.toLowerCase() }
}
