// Ionic reads this before it starts. It picks its look per device: iOS on an iPhone or iPad, Material everywhere else (a desktop,
// an Android phone), with that platform's controls, transitions and gestures. No animation when the system asks for reduced
// motion (or ?still=1, for screenshots from a background tab, where page transitions would wait for frames that never come).
window.Ionic = { config: {
  animated: !((window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) || /[?&]still=1/.test(location.search)) } };
