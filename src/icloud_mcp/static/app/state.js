// What the app keeps between screens: the last result to show on Today, what is being discarded (hidden until Undo runs
// out), whether the server answers, and a request to open once Today is ready (a link or a notification).
export const app = { note: "", noteBad: false, noteUntil: 0, hidden: new Set(), online: true, updated: Date.now() / 1000, pendingOpen: null,
  hintClosed: false, waiting: 0 };
