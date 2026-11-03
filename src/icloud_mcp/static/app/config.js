// Ionic reads this before it starts: iOS look everywhere, and no animation when the system asks for reduced motion
// (or ?still=1, for screenshots from a background tab, where page transitions would wait for frames that never come).
window.Ionic = { config: { mode: "ios", swipeBackEnabled: true,
  animated: !((window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) || /[?&]still=1/.test(location.search)) } };
