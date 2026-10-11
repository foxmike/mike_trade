/** Console routes. The shell owns persistent header/nav; feature pages own
 * their data. Trade inspection is a dedicated route so the selected trade
 * stays visible and navigable. */

import { BrowserRouter, Navigate, Route, Routes } from "react-router";
import AppShell from "./shell/AppShell";
import OperationsPage from "./features/operations/OperationsPage";
import WatchlistPage from "./features/watchlist/WatchlistPage";
import ResearchPage from "./features/research/ResearchPage";
import ResearchRunDetailPage from "./features/research/ResearchRunDetailPage";
import TradeJournalPage from "./features/trades/TradeJournalPage";
import TradeInspectionPage from "./features/trades/TradeInspectionPage";
import SymbolDetailPage from "./features/symbols/SymbolDetailPage";
import AccountDetailPage from "./features/account/AccountDetailPage";
import HealthPage from "./features/health/HealthPage";
import PlannedPage from "./features/placeholder/PlannedPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/operations" replace />} />
          <Route path="/operations" element={<OperationsPage />} />
          <Route path="/watchlist" element={<WatchlistPage />} />
          <Route path="/account" element={<AccountDetailPage />} />
          <Route path="/research" element={<ResearchPage />} />
          <Route path="/research/runs/:selectionId" element={<ResearchRunDetailPage />} />
          <Route path="/trades" element={<TradeJournalPage />} />
          <Route path="/trades/:tradeId" element={<TradeInspectionPage />} />
          <Route path="/symbols/:symbol" element={<SymbolDetailPage />} />
          <Route path="/health" element={<HealthPage />} />
          <Route
            path="/market"
            element={
              <PlannedPage
                title="Market Monitor"
                description="The five-state market risk monitor (NORMAL / WARNING / CRASH / STABILIZING / RECOVERY) with benchmark ETF metrics and transition history."
              />
            }
          />
          <Route
            path="/config"
            element={
              <PlannedPage
                title="Configuration & Control"
                description="Authenticated operator command dispatch into the engine's ControlInbox (pause, disable entries, close position, flatten, stop)."
              />
            }
          />
          <Route path="*" element={<Navigate to="/operations" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
