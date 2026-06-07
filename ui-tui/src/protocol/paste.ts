export const PASTE_SNIPPET_RE = /\[\[[^\n]*?\]\]/g

export interface PasteSnippetRef {
  label: string
  text: string
}

export const expandPasteSnippets = (value: string, snips: PasteSnippetRef[]): string => {
  const byLabel = new Map<string, string[]>()

  for (const { label, text } of snips) {
    const hit = byLabel.get(label)
    hit ? hit.push(text) : byLabel.set(label, [text])
  }

  return value.replace(PASTE_SNIPPET_RE, tok => byLabel.get(tok)?.shift() ?? tok)
}
