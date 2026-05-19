// Stub — replaced in Task 4 with the real top-5 leaderboard. Ships in
// the Task 3 commit so panelRegistry / DEFAULT_PANEL_ORDER / the '0'
// keybinding can be exercised end-to-end against an actual mountable
// React component without an undefined-import build failure.
export function ProjectsPanel() {
  return (
    <section
      className="panel accent-magenta"
      id="panel-projects"
      data-panel-kind="projects"
      role="region"
      aria-label="Projects panel"
    />
  );
}
