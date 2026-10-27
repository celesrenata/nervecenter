/**
 * People & Relationships API client (Personal-Ops Recovery spec §11, workstream J).
 *
 * Typed client for the frozen §11 routes:
 *
 *   GET   /api/people                     → list of canonical people
 *   GET   /api/people/{id}                → one person (+ `relationships` profile
 *                                           when the durable plane is configured)
 *   PATCH /api/people/{id}                → edit durable fields (full-replace editor)
 *   POST  /api/people/{id}/reminders      → create a follow-up reminder (201)
 *   GET   /api/relationships/unanswered  → factual unanswered conversations
 *   GET   /api/relationships/social      → explicit social plans (no fuzzy matching)
 *
 * The shapes below are the exact projections the service emits. The
 * *profile* identity shape (`{ channel, handle, resolved }`) differs from the
 * canonical `Person.identities` (`ContactIdentity`) — both are modelled.
 *
 * House degradation semantics: without a durable registry the backend answers
 * `503 relationships_unavailable` on the durable-only routes and an honest
 * empty list on `GET /api/people`. Mock mode carries a small, clearly
 * labelled fixture set (the same demo-data convention as `mockContacts` in
 * client.ts) so the workspace is demonstrable; it mirrors the wire shapes so
 * the view behaves identically in mock and real mode.
 */

import { apiFetch, apiErrorFromResponse, useMockMode, ApiError } from './client'

// ─── Wire types (exact service projections) ─────────────────────────────────

export interface PersonIdentity {
  kind: string
  value: string
  raw_value?: string | null
  display_name?: string | null
  source?: string
  first_seen_at?: string
  last_seen_at?: string
  [key: string]: unknown
}

/** One canonical person — `GET /api/people` item. */
export interface Person {
  person_id: string
  display_name: string | null
  is_vip: boolean
  identities: PersonIdentity[]
  proposed_merges: string[]
  created_at: string
  updated_at: string
}

export interface PersonNote {
  at: string | null
  text: string
}

export interface PersonBoundary {
  scope: string
  rule: string
  active: boolean
}

export interface Birthday {
  date: string
  name?: string | null
  /** occurrence-augmented fields (present in the profile projection): */
  occurs_on?: string | null
  in_days?: number | null
}

/** Profile identity shape (relationships plane) — differs from `PersonIdentity`. */
export interface ProfileIdentity {
  channel: string
  handle: string
  resolved: boolean
}

export interface Commitment {
  id: string
  text: string
  due_at: string | null
  state: string
  source_communication_id?: string | null
}

export interface PromiseMade {
  id: string
  person_id: string
  display_name?: string | null
  text: string
  due_at: string | null
  state: string
  source_communication_id?: string | null
  created_at?: string | null
}

/** Cross-person unanswered entry — `GET /api/relationships/unanswered` item. */
export interface UnansweredEntry {
  person_id?: string | null
  display_name?: string | null
  channel: string
  last_inbound_at: string
  age_days: number
  subject?: string | null
  content_ref?: string | null
}

/** Per-person unanswered entry — profile projection (no person_id). */
export interface ProfileUnansweredEntry {
  channel: string
  last_inbound_at: string
  age_days: number
  subject?: string | null
  content_ref?: string | null
}

export interface FollowUp {
  id: string
  person_id: string
  display_name?: string | null
  text: string
  due_at: string | null
  is_due: boolean
  state: string
  linked_refs: string[]
  created_at?: string | null
}

export interface SocialPlan {
  event_id: string
  account_id: string
  ical_uid: string
  title: string
  start: string | null
  end: string | null
  all_day: boolean
  location?: string | null
  participants: string[]
  marked_social: boolean
  person_id?: string | null
}

/** The full factual relationship record — `relationships` on `GET /api/people/{id}`. */
export interface PersonProfile {
  person:
    | { person_id: string; display_name: string | null; is_vip: boolean; durable: boolean }
    | null
  identities: ProfileIdentity[]
  notes: PersonNote[]
  boundaries: PersonBoundary[]
  birthdays: Birthday[]
  commitments: Commitment[]
  promises_made: PromiseMade[]
  unanswered: ProfileUnansweredEntry[]
  follow_ups: FollowUp[]
  social_plans: SocialPlan[]
  generated_at: string
}

export interface ApiResponseMeta {
  version: string
  request_id: string
  timestamp: string
  pagination?: { [key: string]: number } | null
}

export interface ListEnvelope<T> {
  data: T[]
  meta: ApiResponseMeta
}

export interface PersonDetail {
  item: Person
  relationships?: PersonProfile
  meta: ApiResponseMeta
}

export interface PersonPatchBody {
  is_vip?: boolean
  notes?: PersonNote[]
  boundaries?: PersonBoundary[]
  birthdays?: Birthday[]
}

export interface PersonPatchResponse {
  item: { person_id: string; display_name: string | null; is_vip: boolean; durable: boolean }
  notes: PersonNote[]
  boundaries: PersonBoundary[]
  birthdays: Birthday[]
  meta: ApiResponseMeta
}

export interface ReminderBody {
  text: string
  due_at?: string | null
  source_ref?: string | null
}

export interface ReminderItem {
  id: string
  person_id: string
  text: string
  due_at: string | null
  state: string
  linked_refs: string[]
  created_at?: string | null
}

export interface ReminderResponse {
  item: ReminderItem
  meta: ApiResponseMeta
}

// ─── Real API implementation ────────────────────────────────────────────────

const enc = (id: string) => encodeURIComponent(id)

export const realPeopleApi = {
  listPeople: async (): Promise<ListEnvelope<Person>> => {
    const res = await apiFetch('/api/people')
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as ListEnvelope<Person>
  },

  getPerson: async (personId: string): Promise<PersonDetail> => {
    const res = await apiFetch(`/api/people/${enc(personId)}`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as PersonDetail
  },

  patchPerson: async (personId: string, body: PersonPatchBody): Promise<PersonPatchResponse> => {
    const res = await apiFetch(`/api/people/${enc(personId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as PersonPatchResponse
  },

  createReminder: async (personId: string, body: ReminderBody): Promise<ReminderResponse> => {
    const res = await apiFetch(`/api/people/${enc(personId)}/reminders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as ReminderResponse
  },

  unanswered: async (opts?: { personId?: string }): Promise<ListEnvelope<UnansweredEntry>> => {
    const qs = opts?.personId ? `?person_id=${enc(opts.personId)}` : ''
    const res = await apiFetch(`/api/relationships/unanswered${qs}`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as ListEnvelope<UnansweredEntry>
  },

  socialPlans: async (opts?: {
    personId?: string
    days?: number
  }): Promise<ListEnvelope<SocialPlan>> => {
    const params = new URLSearchParams()
    if (opts?.personId) params.set('person_id', opts.personId)
    if (opts?.days) params.set('days', String(opts.days))
    const qs = params.toString() ? `?${params.toString()}` : ''
    const res = await apiFetch(`/api/relationships/social${qs}`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as ListEnvelope<SocialPlan>
  },
}

// ─── Mock implementation (small, clearly-labelled demo fixtures) ─────────────

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

function meta(total: number): ApiResponseMeta {
  return {
    version: 'v1',
    request_id: `mock-${Math.random().toString(36).slice(2, 10)}`,
    timestamp: new Date().toISOString(),
    pagination: total >= 0 ? { total } : null,
  }
}

/** Next occurrence of a `YYYY-MM-DD` birthday (this or next year), date-only. */
function nextBirthdayOccurrence(
  date: string,
  today = new Date(),
): { occurs_on: string; in_days: number } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date)
  if (!m) return null
  const month = Number(m[2]) - 1
  const day = Number(m[3])
  const year = today.getFullYear()
  const todayMid = new Date(year, today.getMonth(), today.getDate())
  let candidate = new Date(year, month, day)
  if (candidate.getTime() < todayMid.getTime()) candidate = new Date(year + 1, month, day)
  const inDays = Math.round((candidate.getTime() - todayMid.getTime()) / 86400000)
  return { occurs_on: candidate.toISOString().slice(0, 10), in_days: inDays }
}

interface MockPerson extends Person {
  notes: PersonNote[]
  boundaries: PersonBoundary[]
  birthdays: Birthday[]
  commitments: Commitment[]
  promises: PromiseMade[]
  followups: FollowUp[]
}

const nowIso = new Date().toISOString()

const mockPeople: MockPerson[] = [
  {
    person_id: 'person-alice',
    display_name: 'Alice Rivera',
    is_vip: true,
    identities: [
      { kind: 'email', value: 'alice.rivera@example.com', source: 'personal-relay' },
      { kind: 'phone', value: '+1-555-0142', source: 'personal-relay' },
    ],
    proposed_merges: [],
    created_at: nowIso,
    updated_at: nowIso,
    notes: [{ at: nowIso, text: 'met at the conference' }],
    boundaries: [{ scope: 'general', rule: 'weekends off', active: true }],
    birthdays: [{ date: '1988-03-14', name: 'birthday' }],
    commitments: [
      {
        id: 'commit-alice-1',
        text: 'send the contract',
        due_at: null,
        state: 'open',
        source_communication_id: 'comm-alice-out',
      },
    ],
    promises: [
      {
        id: 'commit-alice-1',
        person_id: 'person-alice',
        display_name: 'Alice Rivera',
        text: 'send the contract',
        due_at: null,
        state: 'open',
        source_communication_id: 'comm-alice-out',
        created_at: nowIso,
      },
    ],
    followups: [
      {
        id: 'followup-alice-1',
        person_id: 'person-alice',
        display_name: 'Alice Rivera',
        text: 'check in about the contract',
        due_at: nowIso,
        is_due: true,
        state: 'open',
        linked_refs: [],
        created_at: nowIso,
      },
    ],
  },
  {
    person_id: 'person-sam',
    display_name: 'Sam Osei',
    is_vip: false,
    identities: [{ kind: 'imessage', value: '+1-555-0199', source: 'personal-relay' }],
    proposed_merges: [],
    created_at: nowIso,
    updated_at: nowIso,
    notes: [],
    boundaries: [],
    birthdays: [],
    commitments: [],
    promises: [],
    followups: [],
  },
]

const mockUnanswered: UnansweredEntry[] = [
  {
    person_id: 'person-alice',
    display_name: 'Alice Rivera',
    channel: 'email',
    last_inbound_at: new Date(Date.now() - 9 * 86400000).toISOString(),
    age_days: 9,
    subject: 'Question about the proposal',
    content_ref: 'enc://i',
  },
]

const mockSocial: SocialPlan[] = [
  {
    event_id: 'acct-1:ical-dinner',
    account_id: 'acct-1',
    ical_uid: 'ical-dinner',
    title: 'Team dinner',
    start: new Date(Date.now() + 2 * 86400000).toISOString(),
    end: new Date(Date.now() + 2 * 86400000 + 3 * 3600000).toISOString(),
    all_day: false,
    location: 'Ristorante',
    participants: ['alice.rivera@example.com'],
    marked_social: true,
    person_id: 'person-alice',
  },
]

function profileFor(p: MockPerson): PersonProfile {
  const birthdays = p.birthdays.map((b) => {
    const occ = nextBirthdayOccurrence(b.date)
    return occ ? { ...b, ...occ } : { ...b }
  })
  return {
    person: {
      person_id: p.person_id,
      display_name: p.display_name,
      is_vip: p.is_vip,
      durable: true,
    },
    identities: p.identities.map((i) => ({
      channel: i.kind,
      handle: i.value,
      resolved: true,
    })),
    notes: p.notes,
    boundaries: p.boundaries,
    birthdays,
    commitments: p.commitments,
    promises_made: p.promises,
    unanswered: mockUnanswered
      .filter((u) => u.person_id === p.person_id)
      .map(({ channel, last_inbound_at, age_days, subject, content_ref }) => ({
        channel,
        last_inbound_at,
        age_days,
        subject,
        content_ref,
      })),
    follow_ups: p.followups,
    social_plans: mockSocial.filter((s) => s.person_id === p.person_id),
    generated_at: new Date().toISOString(),
  }
}

function strip(p: MockPerson): Person {
  const { notes: _n, boundaries: _b, birthdays: _bd, commitments: _c, promises: _pm, followups: _f, ...rest } = p
  void _n
  void _b
  void _bd
  void _c
  void _pm
  void _f
  return rest
}

function findPerson(id: string): MockPerson | undefined {
  return mockPeople.find((p) => p.person_id === id)
}

export const mockPeopleApi = {
  listPeople: async (): Promise<ListEnvelope<Person>> => {
    await delay(60)
    return { data: mockPeople.map(strip), meta: meta(mockPeople.length) }
  },

  getPerson: async (personId: string): Promise<PersonDetail> => {
    await delay(40)
    const p = findPerson(personId)
    if (!p) throw new ApiError(404, `Person ${personId} not found.`)
    return { item: strip(p), relationships: profileFor(p), meta: meta(1) }
  },

  patchPerson: async (personId: string, body: PersonPatchBody): Promise<PersonPatchResponse> => {
    await delay(80)
    const p = findPerson(personId)
    if (!p) throw new ApiError(404, `Person ${personId} not found.`)
    if (body.is_vip !== undefined) p.is_vip = body.is_vip
    if (body.notes !== undefined) p.notes = body.notes
    if (body.boundaries !== undefined) p.boundaries = body.boundaries
    if (body.birthdays !== undefined) p.birthdays = body.birthdays
    p.updated_at = new Date().toISOString()
    const profile = profileFor(p)
    return {
      item: {
        person_id: p.person_id,
        display_name: p.display_name,
        is_vip: p.is_vip,
        durable: true,
      },
      notes: profile.notes,
      boundaries: profile.boundaries,
      birthdays: profile.birthdays,
      meta: meta(1),
    }
  },

  createReminder: async (personId: string, body: ReminderBody): Promise<ReminderResponse> => {
    await delay(80)
    const p = findPerson(personId)
    if (!p) throw new ApiError(404, `Person ${personId} not found.`)
    const item: ReminderItem = {
      id: `followup-${Math.random().toString(36).slice(2, 8)}`,
      person_id: p.person_id,
      text: body.text,
      due_at: body.due_at ?? null,
      state: 'open',
      linked_refs: body.source_ref ? [body.source_ref] : [],
      created_at: new Date().toISOString(),
    }
    p.followups.unshift({ ...item, display_name: p.display_name, is_due: item.due_at !== null })
    return { item, meta: meta(1) }
  },

  unanswered: async (opts?: { personId?: string }): Promise<ListEnvelope<UnansweredEntry>> => {
    await delay(40)
    const rows = opts?.personId ? mockUnanswered.filter((u) => u.person_id === opts.personId) : mockUnanswered
    return { data: rows, meta: meta(rows.length) }
  },

  socialPlans: async (opts?: {
    personId?: string
    days?: number
  }): Promise<ListEnvelope<SocialPlan>> => {
    await delay(40)
    const rows = opts?.personId ? mockSocial.filter((s) => s.person_id === opts.personId) : mockSocial
    return { data: rows, meta: meta(rows.length) }
  },
}

// ─── Exported client (mock-aware) ────────────────────────────────────────────

export const peopleApi = useMockMode ? mockPeopleApi : realPeopleApi
