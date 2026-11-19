import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import { ApiError } from "./lib/api";
import Shell from "./components/Shell";
import Console from "./pages/Console";
import CommandCenterPage from "./pages/CommandCenter";
import OverviewPage from "./pages/Overview";
import CampaignsPage, { CampaignDetailPage } from "./pages/Campaigns";
import ProspectsPage, { ProspectDetailPage } from "./pages/Prospects";
import WebsitesPage, { SiteDetailPage } from "./pages/Websites";
import OutreachPage from "./pages/Outreach";
import ActivityPage from "./pages/Activity";
import SettingsPage from "./pages/Settings";
import IntegrationsPage from "./pages/Integrations";
import VoicePage from "./pages/Voice";
import UsagePage from "./pages/Usage";

const qc = new QueryClient({
  defaultOptions: { queries: { retry: (n, e) => !(e instanceof ApiError && [401, 403, 404].includes(e.status)) && n < 2, staleTime: 3000 } },
});

/** No sign-in screen: the dashboard opens straight to the Command Center. Locally the API runs in its explicit
 *  local-development access mode; if it does not (or is unreachable), Shell shows exactly why. */
export function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<CommandCenterPage />} />
        <Route path="commands" element={<Console />} />
        <Route path="overview" element={<OverviewPage />} />
        <Route path="campaigns" element={<CampaignsPage />} />
        <Route path="campaigns/:id" element={<CampaignDetailPage />} />
        <Route path="prospects" element={<ProspectsPage />} />
        <Route path="prospects/:id" element={<ProspectDetailPage />} />
        <Route path="websites" element={<WebsitesPage />} />
        <Route path="websites/:id" element={<SiteDetailPage />} />
        <Route path="outreach" element={<OutreachPage />} />
        <Route path="activity" element={<ActivityPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="integrations" element={<IntegrationsPage />} />
        <Route path="voice" element={<VoicePage />} />
        <Route path="usage" element={<UsagePage />} />
      </Route>
    </Routes>
  );
}

const el = document.getElementById("root");
if (el) {
  createRoot(el).render(
    <StrictMode><QueryClientProvider client={qc}><BrowserRouter><App /></BrowserRouter></QueryClientProvider></StrictMode>,
  );
}
