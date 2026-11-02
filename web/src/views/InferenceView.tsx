import { useState, useEffect } from 'react'
import { computeApi, routesApi, modelHistoryApi } from '../api/client'
import {
  inferenceApi,
  type ProviderInfo,
  type AvailableInference,
} from '../api/inference'

interface ComputeNode {
  id: string
  hostname: string
  hardware: string
  vram: string
  gpuUtilization: number
  loadedModel: string | null
  tokensPerSec: number
  contextUsage: string | null
  activeCalls: number
  latency: { p50: number; p95: number; p99: number } | null
  failures24h: number
  fallbacks24h: number
}

interface Route {
  routeId: string
  taskType: string
  chain: string[]
  strategy: string
  activeCalls: number
  successRate: number
  avgLatency: number
}

interface ModelHistory {
  model: string
  node: string
  window: string
  avgTokPerSec: number
  p95Latency: number
  successRate: number
  fallbackRate: number
}

interface EmptyStateProps {
  testId: string
  title: string
  body: string
}

/** INF-1: guidance copy for a section that is live but currently empty. */
function EmptyState({ testId, title, body }: EmptyStateProps) {
  return (
    <div
      data-testid={testId}
      className="text-center py-8"
    >
      <p className="text-gray-700 dark:text-gray-300 font-medium">{title}</p>
      <p className="text-sm text-muted mt-2 max-w-md mx-auto">
        {body}
      </p>
    </div>
  )
}

const InferenceView = () => {
  const [nodes, setNodes] = useState<ComputeNode[]>([])
  const [routes, setRoutes] = useState<Route[]>([])
  const [history, setHistory] = useState<ModelHistory[]>([])
  const [loading, setLoading] = useState(true)
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [providersLoading, setProvidersLoading] = useState(true)
  const [providersError, setProvidersError] = useState<string | null>(null)

  // Available models/providers (INFER_CONTROL_CONTRACT.md §1) — lazy: fetched
  // only when the user opens the section, so a plain page load issues no
  // request the current backend may not expose yet.
  const [available, setAvailable] = useState<AvailableInference | null>(null)
  const [availableLoading, setAvailableLoading] = useState(false)
  const [availableError, setAvailableError] = useState<string | null>(null)
  const [availableOpen, setAvailableOpen] = useState(false)

  const loadAvailable = async () => {
    setAvailableLoading(true)
    setAvailableError(null)
    try {
      setAvailable(await inferenceApi.getAvailable())
    } catch (err) {
      setAvailableError(
        err instanceof Error ? err.message : 'Failed to load available models',
      )
    } finally {
      setAvailableLoading(false)
    }
  }

  const toggleAvailable = () => {
    const next = !availableOpen
    setAvailableOpen(next)
    if (next && available === null && !availableLoading) {
      void loadAvailable()
    }
  }

  useEffect(() => {
    const fetchData = async () => {
      try {
        const [nodesRes, routesRes, historyRes] = await Promise.all([
          computeApi.list(),
          routesApi.list(),
          modelHistoryApi.list(),
        ])
        setNodes(nodesRes.data)
        setRoutes(routesRes.data)
        setHistory(historyRes.data)
      } catch {
        // Each of the three sections is fetched independently via Promise.all;
        // a fetch failure is already handled by the per-section empty states,
        // so there is nothing further to surface here.
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [])

  useEffect(() => {
    const fetchProviders = async () => {
      try {
        const res = await inferenceApi.listProviders()
        setProviders(res.data ?? [])
        setProvidersError(null)
      } catch {
        // Honest failure state — the section shows an error, not invented rows.
        setProvidersError('Failed to load providers')
      } finally {
        setProvidersLoading(false)
      }
    }
    fetchProviders()
  }, [])

  const getNodeColor = (util: number) => {
    if (util === 0) return 'bg-gray-500'
    if (util < 50) return 'bg-green-500'
    if (util < 80) return 'bg-yellow-500'
    return 'bg-red-500'
  }

  const getStrategyColor = (strategy: string) => {
    switch (strategy) {
      case 'latency-first':
        return 'bg-blue-500'
      case 'quality-first':
        return 'bg-purple-500'
      case 'cost-first':
        return 'bg-green-500'
      default:
        return 'bg-gray-500'
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-3xl font-bold text-primary">
        Inference
      </h1>

      {/* Providers Section (NERVE_INFERENCE_PROVIDERS registry) */}
      <div className="bg-elevated rounded-lg shadow-lg p-6" data-testid="inference-providers">
        <h3 className="text-xl font-semibold text-primary mb-4">
          Providers
        </h3>
        {providersError ? (
          <div className="text-center py-8">
            <p className="text-red-700 dark:text-red-300 font-medium">{providersError}</p>
            <p className="text-sm text-muted mt-2 max-w-md mx-auto">
              The provider registry could not be reached. Retry the page; the
              list reflects only what is configured in the environment.
            </p>
          </div>
        ) : providersLoading ? (
          <div className="text-center py-12">Loading...</div>
        ) : providers.length === 0 ? (
          <EmptyState
            testId="inference-empty-providers"
            title="No providers configured."
            body="Configure the NERVE_INFERENCE_PROVIDERS environment variable with a JSON array of provider entries (provider, model_id, optional base_url, api_key_env, capabilities) and the registered providers will appear here."
          />
        ) : (
          <div className="space-y-3">
            {providers.map((provider) => (
              <div key={provider.provider} className="bg-hover p-4 rounded-lg">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center space-x-2">
                    <span className="font-medium text-primary">{provider.provider}</span>
                    {provider.default && (
                      <span className="text-xs px-2 py-1 rounded bg-blue-500 text-white">
                        default
                      </span>
                    )}
                  </div>
                  <span
                    className={`text-xs px-2 py-1 rounded font-medium ${
                      provider.key_configured
                        ? 'bg-green-500 text-white'
                        : 'bg-gray-500 text-white'
                    }`}
                  >
                    {provider.key_configured ? 'key configured' : 'no key'}
                  </span>
                </div>
                <div className="text-sm text-secondary mb-2">
                  Model: <span className="text-primary">{provider.model_id}</span>
                </div>
                <div className="flex items-center justify-between text-xs">
                  <div className="flex space-x-3">
                    <span>
                      <span className="text-muted">Base URL:</span>{' '}
                      <span className="text-primary">
                        {provider.base_url ?? '(adapter default)'}
                      </span>
                    </span>
                    <span>
                      <span className="text-muted">Capabilities:</span>{' '}
                      <span className="text-primary">
                        {provider.capabilities.length > 0
                          ? provider.capabilities.join(', ')
                          : 'none declared'}
                      </span>
                    </span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Available models & providers (GET /inference/available, lazy). */}
      <div className="bg-elevated rounded-lg shadow-lg p-6">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-xl font-semibold text-primary">
            Available models & providers
          </h3>
          <button
            type="button"
            data-testid="inference-available-toggle"
            aria-expanded={availableOpen}
            aria-controls="inference-available-panel"
            onClick={toggleAvailable}
            className="text-xs font-medium text-blue-600 dark:text-blue-400 hover:underline"
          >
            {availableOpen ? 'Hide' : 'Show'}
          </button>
        </div>
        <p className="text-sm text-muted mb-4">
          The models and providers you can select for a request or persist on an
          agent, plus the current default. Loaded on demand.
        </p>

        {availableOpen && (
          <div id="inference-available-panel" data-testid="inference-available-panel">
            {availableError ? (
              <div className="text-center py-8">
                <p
                  data-testid="inference-available-error"
                  className="text-red-700 dark:text-red-300 font-medium"
                >
                  {availableError}
                </p>
                <p className="text-sm text-muted mt-2 max-w-md mx-auto">
                  The available-models endpoint could not be reached. Retry the
                  page; nothing is invented here.
                </p>
              </div>
            ) : availableLoading ? (
              <div className="text-center py-12">Loading...</div>
            ) : available ? (
              <div className="space-y-4">
                <div
                  data-testid="inference-available-default"
                  className="flex items-center gap-2 text-sm bg-hover p-3 rounded-lg"
                >
                  <span className="text-muted">Current default:</span>
                  <span className="font-medium text-primary">
                    {available.default.provider} / {available.default.model}
                  </span>
                  <span className="text-xs px-2 py-0.5 rounded bg-blue-500 text-white">
                    default
                  </span>
                </div>
                <div>
                  <h4 className="text-sm font-semibold text-primary mb-2">
                    Providers ({available.providers.length})
                  </h4>
                  {available.providers.length === 0 ? (
                    <EmptyState
                      testId="inference-empty-available"
                      title="No providers available."
                      body="No providers are reachable right now. Configure NERVE_INFERENCE_PROVIDERS and the available options will appear here."
                    />
                  ) : (
                    <ul className="space-y-2">
                      {available.providers.map((p) => (
                        <li
                          key={p.provider}
                          data-testid="inference-available-provider"
                          className="flex items-center gap-2 text-sm bg-hover p-3 rounded-lg"
                        >
                          <span className="font-medium text-primary">
                            {p.provider}
                          </span>
                          {p.default && (
                            <span className="text-xs px-2 py-0.5 rounded bg-blue-500 text-white">
                              default
                            </span>
                          )}
                          <span className="text-xs text-muted ml-auto">
                            {p.default ? `uses ${p.model_id}` : ''}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <div>
                  <h4 className="text-sm font-semibold text-primary mb-2">
                    Models ({available.models.length})
                  </h4>
                  {available.models.length === 0 ? (
                    <p className="text-sm text-muted">No models reported.</p>
                  ) : (
                    <ul className="flex flex-wrap gap-2">
                      {available.models.map((m) => (
                        <li
                          key={m}
                          data-testid="inference-available-model"
                          className="text-sm px-2 py-1 rounded bg-hover text-primary"
                        >
                          {m}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            ) : (
              <div className="text-center py-8 text-sm text-muted">
                Select “Show” to load the available models and providers.
              </div>
            )}
          </div>
        )}
      </div>

      {/* Physical Nodes Section */}
      <div className="bg-elevated rounded-lg shadow-lg p-6">
        <h3 className="text-xl font-semibold text-primary mb-4">
          Physical Nodes
        </h3>
        {loading ? (
          <div className="text-center py-12">Loading...</div>
        ) : nodes.length === 0 ? (
          <EmptyState
            testId="inference-empty-nodes"
            title="No compute nodes reported."
            body="No nodes are currently reporting to the inference layer. Nodes appear here the moment they register and start serving — confirm compute nodes are running and reachable from the API."
          />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {nodes.map((node) => (
              <div key={node.id} className="bg-hover p-4 rounded-lg">
                <div className="flex items-center justify-between mb-2">
                  <h4 className="font-semibold text-primary">{node.hostname}</h4>
                  <div className={`w-2 h-2 rounded-full ${getNodeColor(node.gpuUtilization)}`} />
                </div>
                <div className="text-sm text-secondary mb-2">{node.hardware} • {node.vram}</div>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted">Model:</span>
                  <span className="text-primary">{node.loadedModel || 'Idle'}</span>
                </div>
                <div className="mt-2">
                  <div className="flex justify-between text-xs mb-1">
                    <span className="text-muted">GPU Usage</span>
                    <span className="text-primary">{node.gpuUtilization}%</span>
                  </div>
                  <div className="w-full bg-gray-200 dark:bg-gray-600 rounded-full h-1.5">
                    <div
                      className={`h-1.5 rounded-full ${getNodeColor(node.gpuUtilization)}`}
                      style={{ width: `${node.gpuUtilization}%` }}
                    />
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-2 mt-2 text-xs">
                  <div>
                    <span className="text-muted">Tokens/s:</span>
                    <span className="text-primary ml-1">{node.tokensPerSec}</span>
                  </div>
                  <div>
                    <span className="text-muted">Calls:</span>
                    <span className="text-primary ml-1">{node.activeCalls}</span>
                  </div>
                  <div>
                    <span className="text-muted">p95 Lat:</span>
                    <span className="text-primary ml-1">{node.latency?.p95 || 0}ms</span>
                  </div>
                  <div>
                    <span className="text-muted">Failures (24h):</span>
                    <span className="text-primary ml-1">{node.failures24h}</span>
                  </div>
                  <div>
                    <span className="text-muted">Fallbacks (24h):</span>
                    <span className="text-primary ml-1">{node.fallbacks24h}</span>
                  </div>
                  <div>
                    <span className="text-muted">Context:</span>
                    <span className="text-primary ml-1">{node.contextUsage || 'N/A'}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* OmniRoute Routes Section */}
      <div className="bg-elevated rounded-lg shadow-lg p-6">
        <h3 className="text-xl font-semibold text-primary mb-4">
          OmniRoute Routes
        </h3>
        {loading ? (
          <div className="text-center py-12">Loading...</div>
        ) : routes.length === 0 ? (
          <EmptyState
            testId="inference-empty-routes"
            title="No OmniRoute routes configured."
            body="No routing policies are defined yet. Routes map task types to model chains and appear here once OmniRoute registers a policy for a task type."
          />
        ) : (
          <div className="space-y-3">
            {routes.map((route) => (
              <div key={route.routeId} className="bg-hover p-3 rounded-lg">
                <div className="flex items-center justify-between mb-1">
                  <span className="font-medium text-primary">{route.routeId}</span>
                  <span className={`text-xs px-2 py-1 rounded ${getStrategyColor(route.strategy)} text-white`}>
                    {route.strategy}
                  </span>
                </div>
                <div className="text-xs text-secondary mb-2">
                  Chain: {route.chain.join(' → ')}
                </div>
                <div className="flex items-center justify-between text-xs">
                  <div className="flex space-x-3">
                    <span>
                      Calls: <span className="text-primary">{route.activeCalls}</span>
                    </span>
                    <span>
                      Success: <span className="text-primary">{(route.successRate * 100).toFixed(0)}%</span>
                    </span>
                    <span>
                      Latency: <span className="text-primary">{route.avgLatency}ms</span>
                    </span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Historical Model Performance Section */}
      <div className="bg-elevated rounded-lg shadow-lg p-6">
        <h3 className="text-xl font-semibold text-primary mb-4">
          Historical Model Performance
        </h3>
        {loading ? (
          <div className="text-center py-12">Loading...</div>
        ) : history.length === 0 ? (
          <EmptyState
            testId="inference-empty-history"
            title="No model history recorded."
            body="Per-model performance samples (tok/s, p95 latency, success/fallback rates) appear here once the inference layer has served traffic. This is an honest empty state, not a failed fetch."
          />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {history.map((item, idx) => (
              <div key={idx} className="bg-hover p-3 rounded-lg">
                <div className="font-medium text-primary">{item.model} ({item.node})</div>
                <div className="text-xs text-secondary mb-2">{item.window} window</div>
                <div className="grid grid-cols-2 gap-2 text-xs">
                  <div>
                    <span className="text-muted">Tok/s:</span>
                    <span className="text-primary ml-1">{item.avgTokPerSec}</span>
                  </div>
                  <div>
                    <span className="text-muted">p95 Lat:</span>
                    <span className="text-primary ml-1">{item.p95Latency}ms</span>
                  </div>
                  <div>
                    <span className="text-muted">Success:</span>
                    <span className="text-primary ml-1">{(item.successRate * 100).toFixed(0)}%</span>
                  </div>
                  <div>
                    <span className="text-muted">Fallback:</span>
                    <span className="text-primary ml-1">{(item.fallbackRate * 100).toFixed(0)}%</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export { InferenceView }
