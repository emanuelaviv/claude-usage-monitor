// What switch-advisor keeps in the session's state: the offer its band above the prompt shows after a break.
export type Offer = {
  last: number                         // the reply the break started after (ms since the epoch)
  risk: 'low' | 'medium' | 'high'
  line1: string
  line2: string
  question: string                     // the full card, for "Details"
  options: [string, string]            // the full card's buttons, default first
  numbers?: { resendHere: number; newConv: number; saveNow: number; typical: [number, number]; floor: number }   // logged with the answer, for the guardrail
  test?: boolean                       // shown by /switch-test band
  demo?: boolean                       // /switch-test demo: example numbers only; stays up until a button, /switch-test off or 30 min
}

declare module 'claude-code' {
  interface PluginState {
    'switch-advisor': { offer: Offer | null }
  }
}
