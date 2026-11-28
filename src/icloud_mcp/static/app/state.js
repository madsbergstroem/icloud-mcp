// What the app keeps between screens: the last result to show on Home, what is being discarded (hidden until Undo runs
// out), whether the server answers, the counts the side list shows, a request that is showing how its send went (holding),
// a request to open once Home is ready (a link or a notification), and which part of the waiting list Home shows (filter).
export const app = { note: "", noteBad: false, noteUntil: 0, hidden: new Set(), online: true, updated: Date.now() / 1000, pendingOpen: null,
  hintClosed: false, waiting: 0, paused: false, needs: null, mac: "off", holding: null, filter: "all" };

const SOON = 2 * 3600;      // the server says how soon in /state (needs.soon_seconds); this is only for before it answers

// The groups the side list (wide screens) and the chips over Home (phones) offer, from /state's counts: "Needs you" first, then
// "Asked by" with one entry per agent. A group with nothing in it is left out; "All waiting" always stays.
export function groups(needs) {
  if (!needs) return [];
  const out = [{ id: "all", label: "All waiting", n: needs.all, section: "needs" }];
  if (needs.soon) out.push({ id: "soon", label: "Ends within 2 h", short: "Ends soon", n: needs.soon, section: "needs", tone: "danger" });
  if (needs.look) out.push({ id: "look", label: "Take a close look", short: "Close look", n: needs.look, section: "needs", tone: "warning" });
  for (const [name, n] of needs.by || []) out.push({ id: `by:${name}`, label: name, n, section: "by" });
  return out;
}

// Whether a waiting request (a row of /today) is in a group. The server counts each group by the same signs: `look` is its own
// flag on the row, "soon" is the time left against the same limit, "by" the agent's name.
export function matches(w, filter, now = Date.now() / 1000, soon = (app.needs && app.needs.soon_seconds) || SOON) {
  if (filter === "soon") return w.expires_at - now <= soon;
  if (filter === "look") return !!w.look;
  if (filter.startsWith("by:")) return w.by === filter.slice(3);
  return true;
}

// The filter Home shows, or "all" once its group has gone (nothing in it any more): a refresh keeps the choice while it means something.
export function currentFilter() {
  if (app.filter !== "all" && !groups(app.needs).some((g) => g.id === app.filter)) app.filter = "all";
  return app.filter;
}

export function setFilter(id) {
  app.filter = id;
  window.dispatchEvent(new Event("icm:filter"));
}
