// What the app keeps between screens: the last result to show on Home, what is being discarded (hidden until Undo runs
// out), whether the server answers, the counts the side list shows, a request that is showing how its send went (holding),
// and a request to open once Home is ready (a link or a notification).
export const app = { note: "", noteBad: false, noteUntil: 0, hidden: new Set(), online: true, updated: Date.now() / 1000, pendingOpen: null,
  hintClosed: false, waiting: 0, paused: false, kinds: {}, mac: "off", holding: null };
