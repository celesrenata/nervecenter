import {
  BrowserRouter,
  Routes,
  Route,
} from 'react-router-dom'
import { useSettings } from './theme/SettingsContext'
import { AuthProvider } from './auth/AuthContext'
import { Layout } from './components/Layout'
import { MessagesView } from './views/MessagesView'
import { EmailView } from './views/EmailView'
import { VoicemailView } from './views/VoicemailView'
import { WaitingOnView } from './views/WaitingOnView'
import WellnessView from './views/WellnessView'
import PhysicalHealthView from './views/PhysicalHealthView'
import MentalHealthView from './views/MentalHealthView'
import BillsView from './views/BillsView'
import FinanceView from './views/FinanceView'
import ResearchView from './views/ResearchView'
import ProjectsView from './views/ProjectsView'
import HandoffsView from './views/HandoffsView'
import RoutinesView from './views/RoutinesView'
import { ContactsView } from './views/ContactsView'
import { PeopleView } from './views/PeopleView'
import { CommunicationsView } from './views/CommunicationsView'
import { SecurityReviewView } from './views/SecurityReviewView'
import { SecurityOpsView } from './views/SecurityOpsView'
import { CalendarView } from './views/CalendarView'
import { NowView } from './views/NowView'
import { BriefingView } from './views/BriefingView'
import { OverviewView } from './views/OverviewView'
import { SystemsView } from './views/SystemsView'
import LogsView from './views/LogsView'
import { TimelineView } from './views/TimelineView'
import { GoalsView } from './views/GoalsView'
import { TaskDagView } from './views/TaskDagView'
import { AgentsView } from './views/AgentsView'
import { InferenceView } from './views/InferenceView'
import { ProvidersView } from './views/ProvidersView'
import { InfrastructureView } from './views/InfrastructureView'
import { IncidentsView } from './views/IncidentsView'
import { DecisionsView } from './views/DecisionsView'
import { AskView } from './views/AskView'
import { DownloadsView } from './views/DownloadsView'
import { TodayView } from './views/TodayView'
import { ConnectionsView } from './views/ConnectionsView'
import { OauthCallbackView } from './views/OauthCallbackView'
import { SettingsView } from './views/SettingsView'
import { LoginView } from './views/LoginView'
import { AuthCallback } from './auth/AuthCallback'
import { RequireAuth } from './auth/RequireAuth'

/** `/` renders the feature selected in Settings → Workflow → Home view. */
function HomeView() {
  const { settings } = useSettings()
  if (settings.landingPage === 'overview') return <OverviewView />
  if (settings.landingPage === 'now') return <NowView />
  return <TodayView />
}

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginView />} />
          {/* SSO PKCE authorization-code callback (S102 / AC-S06). Ungated and
              rendered outside Layout so the provider redirect lands standalone
              before the session is established. */}
          <Route path="/auth/callback" element={<AuthCallback />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <Layout />
              </RequireAuth>
            }
          >
            <Route path="/" element={<HomeView />} />
            <Route path="/overview" element={<OverviewView />} />
            <Route path="/connections" element={<ConnectionsView />} />
            {/* Provider-OAuth authorization-code callback (Google / Microsoft
                sign-in). Rendered inside Layout so the session is present and
                the result shows in the app chrome; the wizard's provider
                login is opened in the same tab so the session rides along. */}
            <Route path="/connections/oauth/callback" element={<OauthCallbackView />} />
            <Route path="/now" element={<NowView />} />
            <Route path="/briefing" element={<BriefingView />} />
            <Route path="/calendar" element={<CalendarView />} />
            <Route path="/messages" element={<MessagesView />} />
            <Route path="/email" element={<EmailView />} />
            <Route path="/voicemail" element={<VoicemailView />} />
            <Route path="/contacts" element={<ContactsView />} />
            <Route path="/people" element={<PeopleView />} />
            <Route path="/search" element={<CommunicationsView />} />
            <Route path="/security-review" element={<SecurityReviewView />} />
            <Route path="/security-ops" element={<SecurityOpsView />} />
            <Route path="/waiting-on" element={<WaitingOnView />} />
            <Route path="/wellness" element={<WellnessView />} />
            <Route path="/health" element={<PhysicalHealthView />} />
            <Route path="/mental-health" element={<MentalHealthView />} />
            <Route path="/bills" element={<BillsView />} />
            <Route path="/finance" element={<FinanceView />} />
            <Route path="/research" element={<ResearchView />} />
            <Route path="/projects" element={<ProjectsView />} />
            <Route path="/handoffs" element={<HandoffsView />} />
            <Route path="/routines" element={<RoutinesView />} />
            <Route path="/systems" element={<SystemsView />} />
            <Route path="/logs" element={<LogsView />} />
            <Route path="/timeline" element={<TimelineView />} />
            <Route path="/goals" element={<GoalsView />} />
            <Route path="/tasks" element={<TaskDagView />} />
            <Route path="/agents" element={<AgentsView />} />
            <Route path="/inference" element={<InferenceView />} />
            <Route path="/providers" element={<ProvidersView />} />
            <Route path="/infrastructure" element={<InfrastructureView />} />
            <Route path="/incidents" element={<IncidentsView />} />
            <Route path="/decisions" element={<DecisionsView />} />
            <Route path="/ask" element={<AskView />} />
            <Route path="/downloads" element={<DownloadsView />} />
            <Route path="/settings" element={<SettingsView />} />
          </Route>
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}

export default App