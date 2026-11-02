# NerveCenter Web UI - Features

## Overview

The NerveCenter Web UI provides a comprehensive interface for managing and monitoring your personal intelligence core.

## Core Features

### 1. Overview Dashboard

- Real-time statistics on active goals, agents, tasks, and incidents
- Quick links to manage key areas
- Recent activity summary

### 2. Systems Monitoring

- Health status of all connected systems
- System metrics and version information
- Real-time updates via WebSocket
- System-specific details

### 3. Timeline View

- Chronological event history
- Color-coded severity levels
- Event filtering capabilities
- Timeline navigation

### 4. Goals Management

- Create and track personal objectives
- Progress visualization with progress bars
- Status indicators (active, completed, pending, cancelled)
- Due date tracking

### 5. Task DAG

- Visual task dependency graph
- Task status tracking
- Priority levels
- Execution status (pending, running, completed, failed, cancelled)

### 6. Agent Monitoring

- Agent health and status
- CPU and memory usage visualization
- Task completion statistics
- Last heartbeat information

### 7. Inference Management

- Query the inference provider registry and the selectable model surface
  (`GET /api/inference/available` → providers, models, default; honest empty
  states — never fabricated providers/models)
- Per-request inference control: an optional, collapsed Advanced picker on the
  Ask composer lets you override the model, provider, and the seeding agent for
  a single ask. Omission stays valid — the default path sends a bare
  `{question}` body.
- Per-agent inference binding: each persisted agent carries an optional
  `{provider, model}` binding (`POST/PATCH /api/agents`). Agent binding
  resolution runs at the API layer (never inside the pure `AskService`); a
  request-level override wins over the agent binding, which wins over the
  service default.
- Audit echo: every ask response (`answered` / `policy_denied` /
  `unavailable`) reports the provider/model actually used and its
  `source` (`default` | `agent` | `request`).
- Native agent types cannot be persisted, so they use the service default; the
  Agents view says so instead of silently failing.

### 8. Incident Management

- Report and track incidents
- Severity levels (critical, high, medium, low)
- Status tracking (active, resolved, in-progress)
- Impact assessment
- Tag-based organization

### 9. Ask / Chat Interface

- Interactive Q&A interface
- Context-aware responses
- Quick question suggestions
- Message history

## Technical Features

### Real-time Updates

The Web UI uses WebSocket for real-time data updates, ensuring users always see the latest information.

### Authentication

- Token-based authentication
- Session management
- Role-based access control
- Secure token storage

### Dark Mode Support

All views support dark mode for reduced eye strain in low-light environments.

### Responsive Design

The UI is fully responsive and works on:
- Desktop computers
- Tablets
- Mobile devices

## Settings and preferences

The Settings route groups browser-local preferences into four feature-oriented tabs:

- **Appearance** — system/light/dark theme, accent color, font scale, and motion preference.
- **Workflow** — default briefing kind, default incident severity for new reports, and the home view shown at `/`.
- **Data & privacy** — default Wellness trend window. Wellness collection state, pause windows, exports, retention, and credentials remain server-owned on the Wellness or Connections screens until a settings API is available.
- **Connections & apps** — connector registration/verification guidance and iOS/watchOS download and sideload instructions.

The preference record is persisted under the browser's `nerve-settings` local-storage key. Settings are intentionally not presented as server-synchronized account data. Feature screens consume the workflow defaults when they initialize: Briefing uses the selected briefing kind, Wellness uses the selected trend window, incident reporting uses the selected initial severity, and `/` renders the selected home view. See [SETTINGS.md](SETTINGS.md) for the full key table and ownership boundaries.

## Planned Features

- [ ] Advanced filtering and search
- [ ] Custom dashboard widgets
- [ ] Alert notifications
- [ ] Export functionality
- [ ] Server-synchronized account preferences
- [ ] Multi-language support

## Performance

- Fast initial load with Vite
- Code splitting for optimized bundles
- Lazy loading of views
- Efficient WebSocket connections