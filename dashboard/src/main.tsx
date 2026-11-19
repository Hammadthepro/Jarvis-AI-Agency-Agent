import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import { getToken, onAuthChange } from "./lib/auth";
import { ApiError } from "./lib/api";
import Shell from "./components/Shell";
import Login from "./pages/Login";
import Console from "./pages/Console";
import OverviewPage from "./pages/Overview";
import CampaignsPage, { CampaignDetailPage } from "./pages/Campaigns";
import ProspectsPage, { ProspectDetailPage } from "./pages/Prospects";
import WebsitesPage, { SiteDetailPage } from "./pages/Websites";
import OutreachPage from "./pages/Outreach";
import ActivityPage from "./pages/Activity";
import SettingsPage from "./pages/Settings";

const qc = new QueryClient({
  defaultOptions: { queries: { retry: (n, e) => !(e instanceof ApiError && [401, 403, 404].includes(e.status)) && n < 2, staleTime: 3000 } },
});

export function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  useEffect(() => {
    const check = () => getToken().then((t) => setAuthed(Boolean(t)));
    void check();
    return onAuthChange(() => { qc.clear(); void check(); });
  }, []);
  if (authed === null) return null;
  if (!authed) return <Login />;
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Console />} />
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
