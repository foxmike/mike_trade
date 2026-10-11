/** Placeholder for planned console areas (Market Monitor, Configuration &
 * Control). No functional controls are rendered — the areas are wired into
 * navigation but explicitly marked not-yet-implemented. */

import { PageHeader } from "../../components/ui";

export default function PlannedPage({ title, description }: { title: string; description: string }) {
  return (
    <div className="page">
      <PageHeader title={title} subtitle="Planned — not yet implemented" />
      <div className="panel">
        <div className="state-block state-empty">
          <p>{description}</p>
          <p className="hint">
            This area is reserved in the console architecture. No functional controls are shown
            because the backing endpoints do not exist yet; do not mistake this screen for a
            configured feature.
          </p>
        </div>
      </div>
    </div>
  );
}
