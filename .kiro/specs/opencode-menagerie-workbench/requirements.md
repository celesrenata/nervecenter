# Requirements Document

## Introduction

The Workbench adds a coding surface to the NerveCenter web UI. It covers three things:

1. A UI for opencode and Menagerie projects and sessions.
2. An in-browser code editor for writing code against opencode.
3. An optional browser-based VS Code (code-server) with the Menagerie extension preinstalled.

Project files live on two NFS exports on server 192.168.42.8: `/volume1/Kubernetes/opencode/projects` and `/volume1/Kubernetes/menagerie/projects`. Each export is mounted through its own PersistentVolume and PersistentVolumeClaim.

Existing context this spec builds on (found in the repository):

- The web UI is React 18, Vite, Tailwind and react-router (`web/src`). Views are registered in `web/src/App.tsx` and `web/src/components/Layout.tsx`. The existing `HandoffsView` queues briefs for opencode or Menagerie.
- A central opencode server (`opencode serve`, port 4096, HTTP Basic auth) runs in namespace `opencode` (NS-ADR-106, manifests mirrored in `docs/central-opencode`). Its NetworkPolicy is default-deny. Its workspaces are git clones on a Longhorn PVC.
- The ingress sends everything except named exact paths through oauth2-proxy (Keycloak realm `celestium`).
- Inference goes through OmniRoute and follows the local-first policy (NS-ADR-107). The opencode config uses `omniroute/local/code` and `local/fast`.
- Menagerie is a VS Code extension. Its `packages/ipc` needs VS Code started with `ROO_CODE_IPC_SOCKET_PATH`. Today it runs only on the owner's Mac and handoffs for it are notify-only.
- NS-ADR-106 states that NerveCenter itself runs no code and pushes nothing to a git remote without per-handoff owner approval.

Because the Workbench lets the owner run code and an agent in the cluster, it needs a new ADR that extends NS-ADR-106. Requirement 11 covers that.

Assumptions to confirm during review:

- The Workbench is single-owner. Multi-user access is out of scope.
- The Menagerie extension is installed from a VSIX that the owner supplies. The source and version are unknown to this spec.
- The NFS server permits the cluster nodes to mount both exports.
- The Workbench is an additional surface. It does not replace the Handoffs queue or the Mac-based Menagerie flow.

## Glossary

- **Workbench**: The set of NerveCenter web views, API endpoints and in-cluster services defined by this spec.
- **Workbench_API**: The NerveCenter backend endpoints under `/api/workbench/*` that the Workbench UI calls.
- **Workbench_UI**: The React views of the Workbench inside the NerveCenter web app.
- **Project**: A directory of source files that belongs to exactly one Workspace_Kind and has a unique slug.
- **Workspace_Kind**: Either `opencode` or `menagerie`. It selects the storage root and the tools offered.
- **Project_Store**: The two NFS-backed locations holding project files: the Opencode_Store and the Menagerie_Store.
- **Opencode_Store**: NFS export `192.168.42.8:/volume1/Kubernetes/opencode/projects`.
- **Menagerie_Store**: NFS export `192.168.42.8:/volume1/Kubernetes/menagerie/projects`.
- **Opencode_Server**: The `opencode serve` instance in namespace `opencode`.
- **Session**: One opencode conversation, or one Menagerie task, attached to a Project.
- **Code_Editor**: The in-browser text editor in the Workbench_UI for viewing and editing Project files.
- **Browser_IDE**: A code-server (VS Code in the browser) instance with the Menagerie extension installed.
- **IDE_Instance**: One running Browser_IDE pod bound to one Project.
- **Menagerie_Extension**: The Menagerie VS Code extension installed into the Browser_IDE image.
- **Owner**: The authenticated NerveCenter user, identified by the Keycloak SSO session.
- **Project_Root**: The absolute directory of one Project inside its Project_Store mount.
- **Path_Guard**: The Workbench_API component that resolves a requested path and confirms it stays inside the Project_Root.
- **Audit_Log**: The content-free log of Workbench actions (actor, project, action, outcome).

## Requirements

### Requirement 1: NFS-backed project storage

**User Story:** As the Owner, I want opencode and Menagerie projects stored on my NFS server, so that project files survive pod restarts and live on my NAS.

#### Acceptance Criteria

1. THE Workbench SHALL provide a PersistentVolume that mounts `192.168.42.8:/volume1/Kubernetes/opencode/projects` and a PersistentVolumeClaim that binds to it, such that the PersistentVolumeClaim reaches status `Bound` within 60 seconds of the manifests being applied.
2. THE Workbench SHALL provide a separate PersistentVolume that mounts `192.168.42.8:/volume1/Kubernetes/menagerie/projects` and a separate PersistentVolumeClaim that binds to it, such that the PersistentVolumeClaim reaches status `Bound` within 60 seconds of the manifests being applied.
3. THE Workbench SHALL set `persistentVolumeReclaimPolicy` to `Retain` on both PersistentVolumes, so that deleting either PersistentVolumeClaim leaves the PersistentVolume and all files in its export intact.
4. THE Workbench SHALL declare both PersistentVolumes with access mode `ReadWriteMany`, so that the Opencode_Server and IDE_Instances can mount the same store.
5. THE Workbench SHALL mount the Opencode_Store only into pods that serve the `opencode` Workspace_Kind, and the Menagerie_Store only into pods that serve the `menagerie` Workspace_Kind, such that no pod has both stores mounted.
6. WHEN a Project is created with Workspace_Kind `opencode`, THE Workbench_API SHALL create its Project_Root as a directory under the Opencode_Store, and SHALL NOT create any directory for it under the Menagerie_Store.
7. WHEN a Project is created with Workspace_Kind `menagerie`, THE Workbench_API SHALL create its Project_Root as a directory under the Menagerie_Store, and SHALL NOT create any directory for it under the Opencode_Store.
8. IF a Project_Store mount is unavailable or does not respond to a read or write within 10 seconds, THEN THE Workbench_API SHALL reject the affected Project creation or access request with an error that names the unavailable store, SHALL create no partial Project_Root, SHALL leave existing files unmodified, and SHALL continue to serve requests for the other store.
9. THE Workbench SHALL keep the existing Longhorn PVC `opencode-data` unchanged and SHALL leave its existing handoff clones in place, such that the PVC's spec, bound volume, and contents are identical before and after the NFS storage is deployed.
10. THE Workbench SHALL provide a documented, executable check that, when run from a cluster node, lists both exports, writes a marker file to each export, reads it back with identical content, removes it, and exits with status 0 on success and a non-zero status naming the failing export on any failure.

### Requirement 2: Project catalogue and lifecycle

**User Story:** As the Owner, I want to create, list, open, archive and delete projects of either kind, so that I can manage all my coding work in one place.

#### Acceptance Criteria

1. WHEN the Owner requests the project list, THE Workbench_API SHALL return every Project, including archived Projects, with its slug, Workspace_Kind, title, creation time, last-activity time, size in bytes and archived flag, ordered by last-activity time descending.
2. WHEN the Owner creates a Project with a slug, a title of 1 to 120 characters and a Workspace_Kind, THE Workbench_API SHALL create the Project_Root, record the Project and return the created Project within 10 seconds.
3. IF a slug does not match `^[a-z0-9][a-z0-9-]{0,62}$`, THEN THE Workbench_API SHALL reject the creation with a validation error, SHALL NOT create a Project_Root and SHALL NOT record a Project.
4. IF a title is empty or longer than 120 characters, or the Workspace_Kind is not one of the defined Workspace_Kind values, THEN THE Workbench_API SHALL reject the creation with a validation error, SHALL NOT create a Project_Root and SHALL NOT record a Project.
5. IF a Project with the same slug and Workspace_Kind already exists, THEN THE Workbench_API SHALL reject the creation with a conflict error and SHALL leave the existing Project and its files unchanged.
6. WHEN the Owner creates a Project from a git URL that uses the `https` or `ssh` scheme, THE Workbench_API SHALL clone the repository into the Project_Root and record the Project after the clone completes.
7. IF the git URL uses a scheme other than `https` or `ssh`, THEN THE Workbench_API SHALL reject the creation with a validation error before any clone is attempted, SHALL NOT create a Project_Root and SHALL NOT record a Project.
8. IF a clone fails or does not complete within 300 seconds, THEN THE Workbench_API SHALL abort the clone, remove the partially created Project_Root, SHALL NOT record a Project and SHALL return an error indicating the clone failure or timeout.
9. WHEN the Owner archives a Project, THE Workbench_API SHALL mark it archived, SHALL leave its files on the NFS export unchanged and SHALL keep it in the project list with the archived flag set.
10. WHEN the Owner deletes a Project and the request includes a confirmation field equal to the slug, THE Workbench_API SHALL remove the Project_Root and its record.
11. IF a delete request has a missing confirmation field or one that does not equal the slug, THEN THE Workbench_API SHALL reject the request with a validation error and SHALL leave the Project_Root and its record unchanged.
12. WHEN a directory under a Project_Store has no Project record, THE Workbench_API SHALL list it as an unregistered directory and SHALL offer to import it. The listing SHALL NOT modify any file in that directory.
13. WHEN the Owner imports an unregistered directory, THE Workbench_API SHALL record a Project for it without modifying, moving or deleting any file in the directory.
14. THE Workbench_API SHALL store Project records in PostgreSQL through an additive Alembic migration that creates or adds only, with no drop or alteration of existing tables or columns.
15. FOR ALL valid Project records, creating a Project and then reading it back SHALL return the same slug, title and Workspace_Kind (round-trip property).

### Requirement 3: Project and session views

**User Story:** As the Owner, I want web views for opencode and Menagerie projects and their sessions, so that I can see what is running and resume work.

#### Acceptance Criteria

1. THE Workbench_UI SHALL provide a Workbench entry in the main navigation and a `/workbench` route behind the existing SSO.
2. IF a request to `/workbench` or to any Workbench_API endpoint has no valid SSO session, THEN THE Workbench_UI SHALL redirect the request to the SSO login and THE Workbench_API SHALL reject the request without returning Project or Session data.
3. WHEN the Owner opens `/workbench`, THE Workbench_UI SHALL show Projects grouped by Workspace_Kind, with each group labeled by its Workspace_Kind value and showing its Project count, and SHALL hide archived Projects by default.
4. WHEN the Owner enables the archived Projects filter, THE Workbench_UI SHALL include archived Projects in their Workspace_Kind groups, mark each as archived, and keep the filter state until the Owner disables it or leaves `/workbench`.
5. WHEN the Owner opens a Project, THE Workbench_UI SHALL show its Sessions (each with title, status, and last-updated time), its 10 most recent handoffs from the existing Handoffs queue ordered newest first, and links to the Code_Editor and the Browser_IDE.
6. WHEN the Owner starts a new Session on an `opencode` Project, THE Workbench_API SHALL create the Session on the Opencode_Server with the Project_Root as its working directory and THE Workbench_UI SHALL show the new Session in the Project's Session list within 2 seconds.
7. WHILE a Session is running, THE Workbench_UI SHALL display each streamed Session message and tool-call summary within 2 seconds of its arrival from the Opencode_Server, in the order received.
8. WHEN the Owner sends a message of 1 to 20,000 characters to a running Session, THE Workbench_API SHALL forward it to the Opencode_Server and THE Workbench_UI SHALL show the message in the Session view.
9. IF the Owner submits an empty message, a message longer than 20,000 characters, or a message to a Session that is not running, THEN THE Workbench_UI SHALL reject the submission, show an error with `role="alert"` stating the reason, and keep the entered text.
10. WHEN the Owner aborts a running Session, THE Workbench_API SHALL send the abort request to the Opencode_Server and THE Workbench_UI SHALL show the Session as aborted once the Opencode_Server confirms the abort, or within 10 seconds show an error with `role="alert"` and keep the Session shown as running if no confirmation arrives.
11. WHERE a Project has Workspace_Kind `menagerie`, THE Workbench_UI SHALL show the Menagerie task status reported by the Menagerie_Extension in the Browser_IDE, refreshed at least every 30 seconds while the Project is open.
12. IF the Opencode_Server does not respond within 5 seconds or refuses the connection, THEN THE Workbench_UI SHALL show an "opencode unavailable" state in place of the Session views and SHALL keep the Project list and the Code_Editor link usable.
13. THE Workbench_UI SHALL follow the existing view conventions: Tailwind utility classes, `web/src/api/*` client modules, and `role="alert"` for errors.

### Requirement 4: In-browser code editor for opencode projects

**User Story:** As the Owner, I want to write and edit code directly in the browser for an opencode project, so that I can work without a local checkout.

#### Acceptance Criteria

1. WHEN the Owner opens an `opencode` Project in the Code_Editor, THE Workbench_UI SHALL show a file tree of the Project_Root within 2 seconds for a Project_Root containing up to 10,000 entries, listing directories before files and sorting names in ascending alphabetical order within each group.
2. WHEN the Owner selects a text file within the size limit defined in criterion 6, THE Code_Editor SHALL show its contents within 1 second with syntax highlighting chosen from the file extension, and SHALL show the contents as plain text without highlighting if the extension has no associated language.
3. WHEN the Owner saves a file, THE Workbench_API SHALL write the file to the Project_Root and return the new modification time within 2 seconds for files up to 2 MiB.
4. WHEN the Owner creates, renames, moves or deletes a file or directory, THE Workbench_API SHALL apply the change inside the Project_Root and THE Workbench_UI SHALL update the file tree to reflect the change within 1 second of the Workbench_API confirming it.
5. IF a file has changed on disk since the Code_Editor loaded it, THEN THE Workbench_API SHALL reject the save with a conflict error that carries the current modification time, SHALL leave the on-disk file unchanged, and THE Code_Editor SHALL keep the Owner's unsaved edits in the editor.
6. IF a file is larger than 2 MiB (2,097,152 bytes) or is not valid UTF-8 text, THEN THE Workbench_API SHALL refuse to open it in the Code_Editor and SHALL report its size in bytes and its detected type.
7. WHILE a file has unsaved changes, THE Code_Editor SHALL display an unsaved indicator on that file's tab and in the file tree, and SHALL show a browser confirmation prompt before the Owner closes the tab, reloads the page or navigates away, and SHALL remove the indicator within 500 ms after a successful save.
8. WHEN an opencode Session changes a file in the open Project, THE Code_Editor SHALL show a notice naming the changed file within 5 seconds of the change and SHALL offer a reload action.
9. IF the changed file in criterion 8 has unsaved changes in the Code_Editor, THEN THE Code_Editor SHALL keep the Owner's unsaved edits and SHALL NOT replace them until the Owner chooses the reload action.
10. WHEN the Owner selects text and requests an opencode action, THE Workbench_UI SHALL send the selected text, the file path relative to the Project_Root and the Owner's instruction to the active Session, with the selected text limited to 100,000 characters and the instruction limited to 10,000 characters.
11. IF the Owner requests an opencode action while no Session is active, or the selection or instruction exceeds the limits in criterion 10, THEN THE Workbench_UI SHALL NOT send the request and SHALL display an error indicating the reason.
12. IF a file operation or save in criteria 3 or 4 resolves to a path outside the Project_Root, THEN THE Workbench_API SHALL reject the request with an error indicating the path is outside the Project_Root and SHALL NOT change any file.
13. THE Code_Editor SHALL be bundled with the web build and SHALL make zero requests for scripts, fonts or styles to hosts other than the host serving the Workbench_UI.
14. FOR ALL text files up to 2 MiB, saving a file then reading it back SHALL return identical bytes, including line endings and trailing newline state (round-trip property).

### Requirement 5: Browser-based VS Code with Menagerie

**User Story:** As the Owner, I want an optional VS Code in the browser with Menagerie installed, so that I can use Menagerie without my Mac.

#### Acceptance Criteria

1. WHERE the Browser_IDE is enabled, THE Workbench SHALL provide a container image that contains code-server with the Menagerie_Extension installed and activated, so that the Menagerie_Extension is listed as enabled in the code-server extension list when an IDE_Instance starts.
2. WHERE the Browser_IDE is enabled, THE Workbench SHALL pin the code-server version and the Menagerie_Extension version to exact version identifiers (no "latest" tag or version range) in the image build.
3. WHEN the Owner requests an IDE_Instance for a Project, THE Workbench_API SHALL start exactly one pod that mounts that Project_Root as its workspace folder with read-write access.
4. WHEN an IDE_Instance is requested for a `menagerie` Project, THE Workbench_API SHALL mount the Menagerie_Store into the pod.
5. WHEN an IDE_Instance is requested for an `opencode` Project, THE Workbench_API SHALL mount the Opencode_Store into the pod.
6. WHEN THE Workbench_API starts an IDE_Instance, THE Workbench_API SHALL set the environment variable `ROO_CODE_IPC_SOCKET_PATH` to a socket path inside the pod, so that the Menagerie_Extension IPC is available.
7. WHEN an IDE_Instance reaches the ready state, THE Workbench_UI SHALL open it through a NerveCenter route under `/ide/<project-slug>/`, and requests to that route SHALL reach the IDE_Instance of that Project only.
8. WHEN the Owner stops an IDE_Instance, THE Workbench_API SHALL delete its pod within 30 seconds and SHALL leave the Project files unchanged.
9. WHILE an IDE_Instance has had no HTTP or WebSocket traffic for 60 minutes, THE Workbench_API SHALL stop it by deleting its pod and SHALL leave the Project files unchanged.
10. IF 2 IDE_Instances are already running and the Owner requests a third, THEN THE Workbench_API SHALL reject the request without starting a pod and SHALL return an error indicating that the limit of 2 concurrent IDE_Instances is reached.
11. IF the IDE_Instance does not become ready within 120 seconds, THEN THE Workbench_API SHALL delete the pod, leave the Project files unchanged, and report a startup failure that includes the pod's last event message.
12. WHERE the Browser_IDE is not enabled, THE Workbench_UI SHALL hide the Browser_IDE controls, and THE Code_Editor and opencode Sessions SHALL continue to work.
13. IF the Owner requests an IDE_Instance for a Project that already has a running or starting IDE_Instance, THEN THE Workbench_API SHALL return the existing IDE_Instance and SHALL NOT start a second pod for that Project.

### Requirement 6: Authentication and authorization

**User Story:** As the Owner, I want every Workbench surface behind my SSO, so that nobody else can read or run code in my projects.

#### Acceptance Criteria

1. THE Workbench_API SHALL require a valid, unexpired SSO session for every request to a path beginning with `/api/workbench/`.
2. THE Workbench SHALL route every request to a path beginning with `/ide/` through the existing oauth2-proxy gate.
3. THE Workbench SHALL add no Ingress path that reaches the Workbench_API, an IDE_Instance or the Opencode_Server without passing the SSO check.
4. IF a request to a Workbench endpoint has no valid SSO session, THEN THE Workbench_API SHALL return 401, SHALL perform no file or cluster operation, and SHALL leave all Project files and cluster resources unchanged.
5. THE Workbench_API SHALL read the Opencode_Server password from the existing Secret `nervecenter-opencode` and SHALL NOT include the password in any response body, response header or browser-visible log.
6. THE Workbench_API SHALL start each IDE_Instance with code-server's own password authentication disabled, and SHALL start it only while both the oauth2-proxy gate and a NetworkPolicy that admits only the oauth2-proxy and Workbench_API pods are in effect.
7. WHEN a WebSocket upgrade request arrives for an IDE_Instance, THE Workbench_API SHALL validate the SSO session and confirm that the `Origin` header matches the Workbench's own host before upgrading the connection.
8. IF a WebSocket upgrade request for an IDE_Instance has no valid SSO session or has a missing or non-matching `Origin` header, THEN THE Workbench_API SHALL refuse the upgrade, SHALL return 401 for a missing session or 403 for a bad `Origin`, and SHALL open no connection to the IDE_Instance.
9. THE Workbench_API SHALL accept state-changing operations only through the `POST`, `PUT`, `PATCH` and `DELETE` methods, and SHALL return 405 for a state-changing operation sent with any other method.
10. IF a `POST`, `PUT`, `PATCH` or `DELETE` request to a Workbench endpoint is cross-origin and lacks the existing CSRF protection, THEN THE Workbench_API SHALL return 403 and SHALL perform no file or cluster operation.
11. WHEN a Project create, delete, archive, file write, Session start, IDE start or IDE stop completes or fails, THE Workbench SHALL write one Audit_Log entry containing the actor, project slug, action and outcome (success or failure) within 5 seconds of completion.
12. THE Audit_Log SHALL contain no file contents, no prompts and no secrets, and SHALL cap each entry at 4 KiB.

### Requirement 7: Isolation per project

**User Story:** As the Owner, I want each project isolated from the others, so that one project's code or agent cannot read or damage another.

#### Acceptance Criteria

1. WHEN the Workbench_API receives a file path, THE Path_Guard SHALL resolve it against the Project_Root, following symbolic links, and return the resolved path only if it is inside the Project_Root.
2. IF a received file path resolves outside the Project_Root, is absolute, contains a `..` segment, contains a NUL byte, is longer than 4096 characters, or passes through a symbolic link whose target is outside the Project_Root, THEN THE Path_Guard SHALL reject the path, THE Workbench_API SHALL return an error indicating the path is not permitted, and THE Workbench_API SHALL leave all files unchanged.
3. FOR ALL path strings, including empty strings, strings of 4096 characters or fewer, and strings with Unicode or encoded separators, THE Path_Guard SHALL either return a path inside the Project_Root or reject the path (invariant property).
4. WHEN the Workbench_API starts an IDE_Instance, THE Workbench_API SHALL mount only the selected Project_Root, by `subPath`, into the IDE_Instance, and SHALL NOT mount the whole Project_Store or any other project's Project_Root.
5. WHEN the Workbench_API creates an IDE_Instance pod, THE Workbench_API SHALL configure it to run as a non-zero user ID with `allowPrivilegeEscalation: false`, all Linux capabilities dropped, `readOnlyRootFilesystem: true` except for explicitly declared writable volumes, and the `RuntimeDefault` seccomp profile.
6. WHEN the Workbench_API creates an IDE_Instance pod, THE Workbench_API SHALL set non-zero CPU and memory requests and limits on every container in the pod.
7. THE Workbench SHALL place every IDE_Instance in the dedicated namespace `workbench`, and THE Workbench SHALL apply a default-deny NetworkPolicy covering both ingress and egress to all pods in that namespace.
8. THE Workbench SHALL allow IDE_Instance ingress only from NerveCenter web or API pods, and only on the code-server port.
9. THE Workbench SHALL allow IDE_Instance egress only to cluster DNS, to OmniRoute on TCP port 20128, and to TCP ports 443 and 22 on public addresses, matching the existing opencode egress rules.
10. THE Workbench SHALL deny IDE_Instance connections to the NerveCenter database, the NerveCenter API ports, the Kubernetes API server, the cloud metadata address `169.254.169.254`, and private (RFC 1918) address ranges other than those allowed in criterion 9. A connection attempt to any of these SHALL fail without establishing a connection.
11. WHEN the Workbench_API creates an IDE_Instance pod, THE Workbench_API SHALL set `automountServiceAccountToken: false`, so that no service account token file exists inside the pod.
12. THE Workbench_API SHALL NOT mount or expose to an IDE_Instance any NerveCenter Secret other than the one OmniRoute client key issued for that IDE_Instance, and that key SHALL NOT be usable by any other IDE_Instance.
13. IF a git push or remote change (adding, changing or removing a remote) is attempted in a Project, THEN THE Workbench SHALL apply the same push policy as the central opencode config, so that the attempt is denied with an error indicating push is not approved, unless the Owner has approved push for that Project.

### Requirement 8: Inference routing and cost policy

**User Story:** As the Owner, I want Workbench agents to use my local models first, so that coding sessions follow the existing local-first policy.

#### Acceptance Criteria

1. THE Workbench SHALL configure opencode Sessions and the Menagerie_Extension to send model requests only to OmniRoute.
2. IF an opencode Session or the Menagerie_Extension attempts a model request to a host other than OmniRoute, THEN THE Workbench SHALL block the request, SHALL surface an error to the Owner within 5 seconds indicating that only OmniRoute is permitted, and SHALL keep the Session open with its existing state preserved.
3. WHEN the Owner creates a Session without selecting a model, THE Workbench SHALL set the Session's primary model to `omniroute/local/code` and its small model to `local/fast`.
4. THE Workbench SHALL NOT embed any OpenAI, Anthropic or other provider API key in the Browser_IDE image or the Code_Editor.
5. WHERE the Owner selects a model whose OmniRoute route is not one of the local routes (`local/code`, `local/fast`), THE Workbench_UI SHALL display a "non-local" label on the Session's model selector and require the Owner to confirm before the Session starts.
6. IF the Owner declines the non-local confirmation, THEN THE Workbench SHALL NOT start the Session and SHALL retain the Owner's previous model selection.
7. THE Workbench SHALL provide the OmniRoute client key to each IDE_Instance through a Kubernetes Secret reference and SHALL NOT write the key value to the Project_Store.
8. WHEN a Session completes a model request, THE Workbench_API SHALL record the Session identifier, model name, input token count, output token count and timestamp within 10 seconds, and SHALL NOT record prompt text or completion text.
9. WHEN the Owner opens the existing model-policy panel, THE Workbench_UI SHALL display each Session's recorded model name and token counts alongside the panel's existing entries.

### Requirement 9: Operations and resilience

**User Story:** As the Owner, I want the Workbench to be deployable, observable and recoverable, so that I can run it like the rest of NerveCenter.

#### Acceptance Criteria

1. THE Workbench SHALL provide Kubernetes manifests under `deployments/kubernetes/optional/workbench/`, and THE Workbench SHALL exclude those manifests from the base kustomization until the Owner enables them.
2. THE Workbench SHALL provide in `docs/`, in the style of `docs/central-opencode/README.md`, a numbered enable procedure in which each step names the command to run and the check that confirms the step succeeded, and a numbered rollback procedure that returns the cluster to the state before enablement without deleting data on the NFS exports.
3. WHEN a client sends `GET /api/workbench/health`, THE Workbench_API SHALL respond within 5 seconds with, for each Project_Store, a boolean indicating whether it is mounted, a boolean indicating whether it is writable, and its free space in bytes.
4. IF a Project_Store cannot be probed within 5 seconds, THEN THE Workbench_API SHALL report that Project_Store as not mounted and not writable with free space null, and SHALL still return results for the other Project_Stores.
5. IF free space on a Project_Store is below 5 GiB (5,368,709,120 bytes), THEN THE Workbench_UI SHALL show a low-space warning on the Workbench page that names the affected Project_Store and its free space, and SHALL remove the warning within 60 seconds after free space reaches 5 GiB or more.
6. WHEN an IDE_Instance starts, stops, fails or is stopped for idleness, THE Workbench_API SHALL emit one structured log entry and increment one counter metric for that event type, and THE Workbench_API SHALL include in each log entry the Project identifier, the IDE_Instance identifier, the event type and a timestamp, and SHALL NOT include file contents or file names.
7. WHEN the NerveCenter API restarts, THE Workbench_API SHALL, within 30 seconds of becoming ready, rediscover running IDE_Instances from the cluster by label, and SHALL NOT start a second IDE_Instance for a Project that already has one running.
8. WHEN an IDE_Instance pod is rescheduled, THE Workbench SHALL preserve all Project files, because they reside on the NFS export.
9. THE Workbench SHALL document that NFS snapshots or backups of both exports are the Owner's responsibility, and SHALL document a restore check consisting of the steps to restore a sample Project from backup and confirm that its files match the source.
10. IF the NFS server is unreachable at pod start, THEN THE Workbench SHALL leave the pod in the Pending state, SHALL make a Kubernetes event describing the mount failure visible on the pod, and SHALL NOT fall back to local ephemeral storage.
11. THE Workbench SHALL set the NFS mount options `nfsvers=4.1` and `hard` in each PersistentVolume, and SHALL document these values and the reason for choosing them.
12. WHILE 4 file-write requests for a Project are in progress, THE Workbench_API SHALL reject each additional file-write request for that Project with an error indicating that the concurrency limit was reached, and SHALL leave the files from the in-progress requests unchanged by the rejected requests.

### Requirement 10: Relationship to handoffs and the Mac flow

**User Story:** As the Owner, I want the Workbench to work alongside the existing handoff queue, so that I do not lose the current workflow.

#### Acceptance Criteria

1. THE Workbench SHALL keep the request and response behaviour of every existing `/api/handoffs/*` endpoint, and the Handoffs view behaviour, identical to their behaviour before the Workbench was added. This covers accepted inputs, returned fields, status values, and state transitions.
2. WHEN the Owner starts a Session from a Project, THE Workbench_API SHALL NOT create, modify, or delete any handoff row, so the handoff row count and the contents of existing rows are the same before and after the Session starts.
3. WHERE a handoff targets `menagerie` and a `menagerie` Project exists whose slug equals the handoff repo's slug, THE Workbench_UI SHALL display an "Open in Browser IDE" link on that handoff, and the link SHALL open that Project's Workbench view.
4. IF a handoff targets `menagerie` and no `menagerie` Project exists whose slug equals the handoff repo's slug, THEN THE Workbench_UI SHALL NOT display the "Open in Browser IDE" link on that handoff.
5. WHEN the Owner activates the "Open in Browser IDE" link, THE Workbench_UI SHALL NOT change the handoff's status or any other handoff field.
6. THE Workbench SHALL keep the Mac `HandoffAgent` behaviour identical to its behaviour before the Workbench was added. This covers which handoffs it claims, how it processes them, and which status values it writes.

### Requirement 11: Decision record and delivery gates

**User Story:** As the Owner, I want the security-relevant decisions recorded and delivered in phases, so that the new in-cluster code execution is reviewed before it is enabled.

#### Acceptance Criteria

1. THE Workbench SHALL contain, in `.kiro/specs/nerve-shield/decisions.md`, one new ADR entry that has a unique identifier not used by any existing entry, a status field, and separate sections that each record (a) the extension of NS-ADR-106 to in-cluster interactive code execution, (b) the storage design, and (c) the isolation model.
2. THE Workbench SHALL deliver in three sequential phases: Phase 1 storage and catalogue, Phase 2 Code_Editor with opencode Sessions, Phase 3 Browser_IDE.
3. WHILE a delivery phase is not marked complete, THE Workbench SHALL keep the next phase's features disabled in every deployed environment.
4. WHEN a delivery phase completes, THE Workbench SHALL record verification evidence that lists the automated test run results for that phase (test count, pass count, fail count) and the date of the run, and the Owner SHALL review it before the next phase starts.
5. IF the recorded verification evidence for a phase shows one or more failing tests, THEN THE Workbench SHALL keep that phase marked incomplete and SHALL keep the next phase disabled.
6. THE Workbench SHALL provide automated tests that verify all of the following: Path_Guard rejects a path containing `..` segments, an absolute path, a path that resolves outside the Project_Root through a symlink, and a path containing a null byte; Project slug validation accepts valid slugs and rejects empty, over-length and disallowed-character slugs; a Project that is created, saved and then read back returns identical content; and a request without valid authentication is rejected and no Project data is returned or changed.
7. WHEN the Playwright end-to-end test runs, THE Workbench SHALL pass a test that opens the Code_Editor for a Project, edits a file, saves it, reloads the page, and finds the saved file content unchanged in the Code_Editor.

## Open Questions for the Owner

- Where do the Menagerie_Extension VSIX and its source live, and which version should the image pin?
- Should the Browser_IDE be per Project (as written) or one shared instance for each Workspace_Kind?
- Should the Code_Editor also support `menagerie` Projects, or only `opencode` Projects (as written)?
- Does the NFS export squash users, and which UID and GID should own project files?
- Is a Monaco-class editor acceptable as a new web dependency? The web app currently has no editor library.
