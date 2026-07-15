---
name: humanizer
description: "Humanize text: strip AI-isms and add real voice."
version: 2.5.1
author: Siqi Chen (@blader, https://github.com/blader/humanizer), ported by Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [writing, editing, humanize, anti-ai-slop, voice, prose, text]
    category: creative
    homepage: https://github.com/blader/humanizer
    related_skills: [songwriting-and-ai-music]
---

# Humanizer

Use this skill to rewrite text so it sounds like a specific person or a natural human author rather than a generic LLM.

Also use it as a final pass for user-facing prose you create: documentation, PR descriptions, summaries, emails, posts, bios, and long explanations.

## Prerequisites

- Preserve the user's meaning, facts, constraints, and required tone.
- Ask for voice samples only when matching a specific voice matters and samples are not already present.
- For factual or legal/medical/financial text, do not make claims more certain than the source.

## Workflow

1. Identify audience, channel, author persona, and stakes.
2. Remove AI tells: filler transitions, symmetry, generic intensifiers, empty caveats, excessive lists, and moralizing wrap-ups.
3. Add human texture where appropriate: concrete nouns, varied sentence length, lived constraints, precise verbs, and natural rhythm.
4. Preserve useful structure, but break formulaic "first/second/finally" scaffolding when it reads canned.
5. Check that the output still says the same thing and does not over-polish away the author's voice.
6. If editing someone else's text, provide either the revised text only or a compact change rationale, depending on the user request.

## Quick checklist

- Replace vague praise with specific observation.
- Replace "delve, tapestry, realm, robust, seamless, leverage" style diction unless genuinely apt.
- Avoid "not only X but Y" and "it is important to note" unless they earn their keep.
- Keep contractions and fragments when they fit the speaker.
- Prefer concrete examples over abstract claims.
- End where the thought ends; do not append a generic concluding paragraph.

## Reference map

- [references/full-guide.md](references/full-guide.md) — archived full guide with extensive AI-writing pattern catalog, before/after examples, and voice-matching heuristics.
- `LICENSE` — upstream license for the original humanizer material.

Load the full guide for deep reviews, difficult voice matching, long-form edits, or when you need the expanded pattern list.

## Pitfalls

- Do not fabricate anecdotes, credentials, citations, or emotions.
- Do not make professional text sloppy just to make it "human."
- Do not remove accessibility, safety, compliance, or attribution language that the context requires.
- Do not flatten a strong authorial voice into bland conversational prose.

## Verification

- Re-read against the source for meaning preservation.
- Check that specific claims, numbers, names, and citations survived.
- Read the result aloud for rhythm and obvious AI cadence.
