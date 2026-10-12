/** Research Lab: historical research only. The 100-symbol research universe,
 * indexed backtests and run analytics live here; none of it is the active
 * SHADOW watchlist. View, filters, sort and paging are URL state, so Back
 * from an experiment returns to the exact list position. */

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import "./research.css";
import { PageHeader } from "../../components/ui";
import { refreshResearchCatalog } from "../../api/research";
import CompareTab from "./CompareTab";
import ExplorerTab from "./ExplorerTab";
import PerformanceTab from "./PerformanceTab";
import UniverseTab from "./UniverseTab";
import { useLabState } from "./labState";
import { LAB_VIEWS } from "./urlState";

export default function ResearchPage() {
  const lab = useLabState();
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  const [refreshNotice, setRefreshNotice] = useState<string | null>(null);

  const handleRefresh = async () => {
    try {
      setRefreshing(true);
      const res = await refreshResearchCatalog();
      await queryClient.invalidateQueries({
        predicate: (query) => {
          const key = query.queryKey[0];
          return (
            typeof key === "string" &&
            (key.startsWith("lab-") || key.startsWith("research"))
          );
        },
      });
      setRefreshNotice(`Catalog refreshed (${res.indexed_runs} runs indexed)`);
      setTimeout(() => setRefreshNotice(null), 4000);
    } catch (err) {
      setRefreshNotice(err instanceof Error ? err.message : "Refresh failed");
      setTimeout(() => setRefreshNotice(null), 5000);
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="page page-wide rl">
      <PageHeader
        title="Research Lab"
        subtitle="Historical backtests and datasets — separate from the live SHADOW watchlist and operational trading"
        actions={
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            {refreshNotice ? <span className="hint">{refreshNotice}</span> : null}
            <button
              type="button"
              className="button"
              onClick={() => void handleRefresh()}
              disabled={refreshing}
              title="Trigger incremental catalog discovery for newly added research runs"
            >
              {refreshing ? "Refreshing…" : "↻ Refresh catalog"}
            </button>
          </div>
        }
      />
      <div className="rl-tabs" role="tablist" aria-label="Research Lab views">
        {LAB_VIEWS.map((view) => (
          <button
            key={view.id}
            type="button"
            role="tab"
            id={`rl-tab-${view.id}`}
            aria-selected={lab.view === view.id}
            aria-controls={`rl-panel-${view.id}`}
            className={lab.view === view.id ? "active" : ""}
            onClick={() => lab.setView(view.id)}
          >
            {view.label}
            {view.id === "compare" && lab.compareIds.length > 0 ? (
              <span className="rl-count">{lab.compareIds.length}</span>
            ) : null}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`rl-panel-${lab.view}`} aria-labelledby={`rl-tab-${lab.view}`} className="rl-panel">
        {lab.view === "explorer" ? <ExplorerTab lab={lab} /> : null}
        {lab.view === "performance" ? <PerformanceTab lab={lab} /> : null}
        {lab.view === "compare" ? <CompareTab lab={lab} /> : null}
        {lab.view === "universe" ? <UniverseTab lab={lab} /> : null}
      </div>
    </div>
  );
}
