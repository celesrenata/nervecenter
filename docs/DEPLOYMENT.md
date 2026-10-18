# NerveCenter Kubernetes Deployment Guide

## Overview

This document provides a comprehensive guide for deploying NerveCenter to a Kubernetes cluster. The deployment consists of several components: PostgreSQL with pgvector, NATS server, API service, MCP server, and web UI.

## Prerequisites

1. Kubernetes cluster with Traefik Ingress Controller and cert-manager installed
2. Docker registry (Harbor) with proper permissions
3. Keycloak instance for SSO (Single Sign-On)
4. kubectl configured to access your cluster

## Deployment Steps

### 1. Create Namespace

Apply the namespace configuration:

```bash
kubectl apply -f deployments/kubernetes/base/namespace.yaml
```

### 2. Configure Secrets

1. Create `secrets.yaml` from the template:

```bash
cp deployments/kubernetes/base/secrets.yaml.example secrets.yaml
```

2. Edit `secrets.yaml` with your actual secrets:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: nervecenter-env
  namespace: nervecenter
type: Opaque
data:
  NERVE_DB_URL: "<base64-encoded-postgres-connection-string>"
  NERVE_NATS_URL: "<base64-encoded-nats-connection-string>"
  NERVE_OMNIROUTE_URL: "<base64-encoded-omniroute-url>"
  NERVE_MOCK_INFER: "<base64-encoded-mock-infer-value>"
  NERVECENTER_DATABASE_URL: "<base64-encoded-postgres-connection-string>"
  NERVECENTER_NATS_URL: "<base64-encoded-nats-connection-string>"
  NERVECENTER_MOCK_INFER: "<base64-encoded-mock-infer-value>"
```

3. Apply the secrets:

```bash
kubectl -n nervecenter apply -f secrets.yaml
```

### 3. Set Up Image Pull Secret

Create the Harbor pull secret:

```bash
kubectl -n nervecenter create secret generic harbor-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=$HOME/.docker/config.json
```

### 4. Deploy Database

Apply the PostgreSQL configuration:

```bash
kubectl apply -f deployments/kubernetes/base/postgres-pvc.yaml
kubectl apply -f deployments/kubernetes/base/postgres-statefulset.yaml
kubectl apply -f deployments/kubernetes/base/postgres-service.yaml
```

### 5. Deploy Message Queue

Apply the NATS configuration:

```bash
kubectl apply -f deployments/kubernetes/base/nats-deployment.yaml
kubectl apply -f deployments/kubernetes/base/nats-service.yaml
```

### 6. Run Database Migrations

Apply the migration job:

```bash
kubectl apply -f deployments/kubernetes/base/migrate-job.yaml
```

### 7. Deploy API Service

Apply the API deployment and service:

```bash
kubectl apply -f deployments/kubernetes/base/api-deployment.yaml
kubectl apply -f deployments/kubernetes/base/api-service.yaml
```

### 8. Deploy MCP Server

Apply the MCP deployment and service:

```bash
kubectl apply -f deployments/kubernetes/base/mcp-deployment.yaml
kubectl apply -f deployments/kubernetes/base/mcp-service.yaml
```

### 9. Deploy Web UI

Apply the web deployment and service:

```bash
kubectl apply -f deployments/kubernetes/base/web-deployment.yaml
kubectl apply -f deployments/kubernetes/base/web-service.yaml
```

### 10. Set Up SSO

1. Create the OAuth secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: nervecenter-oauth
  namespace: nervecenter
type: Opaque
data:
  client-secret: "<base64-encoded-keycloak-client-secret>"
  cookie-secret: "<base64-encoded-cookie-secret>"
```

2. Apply the OAuth configuration:

```bash
kubectl apply -f deployments/kubernetes/base/oauth-deployment.yaml
kubectl apply -f deployments/kubernetes/base/oauth-service.yaml
```

### 11. Configure Ingress

Apply the Ingress configuration:

```bash
kubectl apply -f deployments/kubernetes/base/ingress.yaml
```

## Verification

After deployment, verify each component:

1. Check PostgreSQL:

```bash
kubectl -n nervecenter exec -it $(kubectl -n nervecenter get pods -l app=postgres -o jsonpath='{.items[0].metadata.name}') -- psql -U nervcenter -d nervcenter -c "SELECT version();"
```

2. Check NATS:

```bash
kubectl -n nervecenter exec -it $(kubectl -n nervecenter get pods -l app=nats -o jsonpath='{.items[0].metadata.name}') -- nats server report
```

3. Check API:

```bash
kubectl -n nervecenter port-forward svc/api 8000:8000
curl http://localhost:8000/api/health
```

4. Check Web UI:

```bash
kubectl -n nervecenter port-forward svc/web 8080:80
open http://localhost:8080
```

## Troubleshooting

If you encounter issues:

1. Check pod logs:

```bash
kubectl -n nervecenter logs -l app=<component-name>
```

2. Describe the pod for events:

```bash
kubectl -n nervecenter describe pod <pod-name>
```

3. Check the status of all resources:

```bash
kubectl -n nervecenter get all
```

## Notes

- The deployment assumes you have proper DNS records pointing to your cluster's ingress controller.
- Adjust resource requests and limits in the deployment files according to your cluster's capacity.
- For production deployments, consider adding horizontal pod autoscaling and proper monitoring.

## Kustomize Deployment

For a more streamlined deployment, you can use Kustomize:

```bash
kubectl apply -k deployments/kubernetes
```

This will apply all the resources in the base directory in a single command.
