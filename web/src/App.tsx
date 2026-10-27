import {
  BrowserRouter,
  Routes,
  Route,
} from 'react-router-dom'
import { AuthProvider } from './auth/AuthContext'
import { Layout } from './components/Layout'
import { MessagesView } from './views/MessagesView'
import { EmailView } from './views/EmailView'
import { VoicemailView } from './views/VoicemailView'
import { WaitingOnView } from './views/WaitingOnView'
import WellnessView from './views/WellnessView'
import BillsView from './views/BillsView'
import RoutinesView from './views/RoutinesView'
import { ContactsView } from './views/ContactsView'
import { PeopleView } from './views/PeopleView'
import { CalendarView } from './views/CalendarView'
import { NowView } from './views/NowView'
import { BriefingView } from './views/BriefingView'
import { OverviewView } from './views/OverviewView'
import { SystemsView } from './views/SystemsView'
import { TimelineView } from './views/TimelineView'
import { GoalsView } from './views/GoalsView'
import { TaskDagView } from './views/TaskDagView'
import { AgentsView } from './views/AgentsView'
import { InferenceView } from './views/InferenceView'
import { IncidentsView } from './views/IncidentsView'
import { DecisionsView } from './views/DecisionsView'
import { AskView } from './views/AskView'
import { DownloadsView } from './views/DownloadsView'
import { TodayView } from './views/TodayView'
import { ConnectionsView } from './views/ConnectionsView'
import { SettingsView } from './views/SettingsView'
import { LoginView } from './views/LoginView'
import { AuthCallback } from './auth/AuthCallback'

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
              <Layout />
            }
          >
            <Route path="/" element={<TodayView />} />
            <Route path="/overview" element={<OverviewView />} />
            <Route path="/connections" element={<ConnectionsView />} />
            <Route path="/now" element={<NowView />} />
            <Route path="/briefing" element={<BriefingView />} />
            <Route path="/calendar" element={<CalendarView />} />
            <Route path="/messages" element={<MessagesView />} />
            <Route path="/email" element={<EmailView />} />
            <Route path="/voicemail" element={<VoicemailView />} />
            <Route path="/contacts" element={<ContactsView />} />
            <Route path="/people" element={<PeopleView />} />
            <Route path="/waiting-on" element={<WaitingOnView />} />
            <Route path="/wellness" element={<WellnessView />} />
            <Route path="/bills" element={<BillsView />} />
            <Route path="/routines" element={<RoutinesView />} />
            <Route path="/systems" element={<SystemsView />} />
            <Route path="/timeline" element={<TimelineView />} />
            <Route path="/goals" element={<GoalsView />} />
            <Route path="/tasks" element={<TaskDagView />} />
            <Route path="/agents" element={<AgentsView />} />
            <Route path="/inference" element={<InferenceView />} />
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