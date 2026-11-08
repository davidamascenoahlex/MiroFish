# MiroFish Security Hardening Implementation Plan

> **For agentic workers:** Follow this plan task-by-task. Keep every command and edit inside the Kubernetes workspace Pod. Use checkbox (`- [ ]`) steps; do not overwrite or reset existing worktree changes.

**Goal:** Harden the current self-hosted Graphiti worktree against the confirmed frontend-dev-server and graph-scope issues while making Neo4j transport security explicit.

**Architecture:** Build the Vue frontend into static assets served by a production Nginx container, with `/api` proxied to a private Flask/Gunicorn backend. Keep the API unauthenticated only because the operator explicitly chose to trust the external VPN/firewall; expose no backend or Neo4j service port directly. Scope Graphiti operations to the requested group and graph-owned records, and require authenticated TLS for remote Neo4j.

**Tech Stack:** Kubernetes workspace Pod, Docker multi-stage builds, Vue/Vite, Nginx, Flask/Gunicorn, Graphiti, Neo4j, pytest, npm.

## Global Constraints

- Keep API authentication out of scope; the external VPN/firewall restriction has not been independently verified by the current Kubernetes RBAC.
- Build the frontend as static production assets. Serve it through an Nginx proxy; route `/api` to the backend over the Pod-local interface. Expose only the proxy port through any NodePort; do not expose the Flask or Neo4j ports directly.
- Keep Neo4j dedicated to MiroFish. Restrict Graphiti reads and destructive graph operations to the requested `group_id` and Graphiti-owned records.
- Require authenticated TLS (`bolt+s://`) for remote Neo4j URIs. Permit plaintext Bolt only for loopback connections within the same Pod.
- Keep LLM and embedding credentials independent; preserve the current requirement and its regression test.
- Run all reads, edits, builds, and tests in `/workspace/MiroFish` through `kubectl exec` to `mirofish-workbench-0`; do not modify the host checkout or apply cluster resources.
- Preserve all pre-existing uncommitted files and changes. Do not commit.

---

## File map

- `backend/app/services/graphiti_adapter.py`: Graphiti node lookup and graph deletion boundaries.
- `backend/tests/test_graphiti_adapter_security.py`: regression tests for group isolation and delete query scope.
- `backend/app/config.py`: Neo4j URI transport-security validation.
- `backend/tests/test_graphiti_config.py`: configuration policy tests, including no embedding-key fallback.
- `backend/pyproject.toml`, `backend/requirements.txt`, `backend/uv.lock`: production WSGI server dependency.
- `backend/wsgi.py`: validated Gunicorn application entry point.
- `backend/gunicorn.conf.py`: one-worker production server defaults, binding to loopback for same-Pod proxying.
- `Dockerfile`: frontend build plus distinct production backend/frontend image targets; no Vite dev startup.
- `deploy/nginx/default.conf.template`: static SPA serving and private API proxy configuration.
- `docker-compose.yml`: production frontend/backend services, publish only the Nginx port.
- `.env.example`: Graphiti, Neo4j, and independent embedding settings without Zep credentials.
- `docs/architecture/self-hosted-deployment.md`: production routing and accepted external-firewall boundary.

## Task 1: Enforce Graphiti group boundaries

**Files:**
- Modify: `backend/app/services/graphiti_adapter.py`
- Create: `backend/tests/test_graphiti_adapter_security.py`

**Interfaces:**
- Preserve `GraphitiClient` and its synchronous adapter API.
- Keep `_GraphitiBackend.delete_group(graph_id: str)` as the delete entry point.
- Graph deletion must match only Graphiti node labels `Entity`, `Episodic`, and `Community`, and must parameterize `group_id`.
- An entity fetched by UUID must not be returned unless its `group_id` equals the requested graph ID.

- [ ] **Step 1: Add a delete-query regression test.** Use a fake async driver that captures `execute_query(query, **params)`, invoke `asyncio.run(_GraphitiBackend.delete_group(...))`, and assert the parameter is exactly the requested group and the query restricts nodes to the three Graphiti labels before `DETACH DELETE`.
- [ ] **Step 2: Run the focused test and confirm it fails** because the current query matches every node with the group ID.

Run inside the Pod: `cd /workspace/MiroFish/backend && uv run pytest tests/test_graphiti_adapter_security.py -q`

Expected: the delete-scope assertion fails; no Neo4j service is needed because the driver is fake.

- [ ] **Step 3: Add a cross-group entity lookup test.** Stub `EntityNode.get_by_uuid` and the backend runner with a node whose `group_id` differs from the request; assert `_NodeNamespace.get(uuid_, graph_id=...)` returns `None`. Add the matching-group case and assert it returns the node wrapper.
- [ ] **Step 4: Implement only the graph-label restriction** in `delete_group`; keep the graph ID as a Cypher parameter and do not interpolate it into query text.
- [ ] **Step 5: Run the focused tests again** and confirm both group mismatch and query-scope tests pass.

Run: `cd /workspace/MiroFish/backend && uv run pytest tests/test_graphiti_adapter_security.py -q`

Expected: all adapter security tests pass.

## Task 2: Require TLS for remote Neo4j

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/tests/test_graphiti_config.py`

**Interfaces:**
- Preserve `Config.validate() -> list[str]`.
- Plaintext `bolt://` is allowed only for loopback. Reject plaintext `neo4j://` routing even for a loopback seed because it can discover remote servers.
- Remote connections must use certificate-verified `bolt+s://` or `neo4j+s://`; reject plaintext and self-signed-only `+ssc` schemes.

- [ ] **Step 1: Add tests** for loopback plaintext accepted, remote plaintext rejected, remote `bolt+s://` accepted, and remote `bolt+ssc://` rejected.
- [ ] **Step 2: Run `uv run pytest tests/test_graphiti_config.py -q`** and confirm the remote-plaintext test fails before the implementation.
- [ ] **Step 3: Add a small URI validation helper** in `config.py` using `urllib.parse.urlparse`; append an explicit validation error from `Config.validate()` when the scheme/hostname pair violates the policy.
- [ ] **Step 4: Run the config tests** and confirm all URI cases pass, including the existing independent embedding credential test.

## Task 3: Replace Vite dev runtime with production static proxy

**Files:**
- Modify: `Dockerfile`
- Create: `deploy/nginx/default.conf.template`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `backend/pyproject.toml`, `backend/requirements.txt`, `backend/uv.lock`
- Create: `backend/wsgi.py`
- Create: `backend/gunicorn.conf.py`
- Modify: `docs/architecture/self-hosted-deployment.md`

**Interfaces:**
- Docker build targets: `backend` (Flask/Gunicorn on loopback port 5001 by default) and `frontend` (Nginx on 8080).
- Nginx reads `BACKEND_URL`; Docker Compose uses `http://mirofish-backend:5001`, while a Kubernetes sidecar uses `http://127.0.0.1:5001`.
- Frontend build uses `VITE_API_BASE_URL=/` so browser requests are same-origin.
- Docker Compose publishes only `3000:8080`; do not publish backend 5001 or Neo4j Bolt/Browser ports beyond loopback.

- [ ] **Step 1: Add the Nginx template** with `listen 8080`, static-root fallback to `/index.html`, `/api/` proxy to `${BACKEND_URL}`, and proxy buffering disabled for streaming API routes.
- [ ] **Step 2: Convert `Dockerfile` into multi-stage targets.** Build `frontend/dist` using Node and `npm ci`; create the `backend` target from Python with the frozen uv environment and `gunicorn`; create the `frontend` target from `nginx:1.28-alpine` with the static build and template. Remove `CMD ["npm", "run", "dev"]` from production runtime.
- [ ] **Step 3: Add `backend/gunicorn.conf.py`** with a single `gthread` worker, eight threads, a 600-second timeout, and default bind `127.0.0.1:5001`; Compose overrides the bind to the private container-network interface.
- [ ] **Step 4: Add `backend/wsgi.py`** that runs `Config.validate()`, raises a startup error listing invalid setting names (never values), then exports `application = create_app()` for Gunicorn.
- [ ] **Step 5: Add Gunicorn to the backend dependency declarations** and regenerate `backend/uv.lock` from inside the Pod with `uv lock`.
- [ ] **Step 6: Update Compose** to build the two targets, route the frontend proxy to the backend service by its Compose DNS name, and publish only Nginx's port 8080 as host port 3000.
- [ ] **Step 7: Replace the Zep-only example settings** with Graphiti/Neo4j and independent embedding values; document loopback-only plaintext Bolt and TLS-required remote Neo4j.
- [ ] **Step 8: Update the deployment guide** to state that any Kubernetes NodePort must select only Nginx, Flask and Neo4j ports stay private, and API access remains unauthenticated and depends on the external VPN/firewall.
- [ ] **Step 9: Validate** with `cd /workspace/MiroFish/frontend && npm run build` and `cd /workspace/MiroFish/backend && uv run pytest -q`; inspect the Dockerfile and Nginx template for the configured production targets and routes. Docker and Compose are not installed in this Pod, so container-image builds and `docker compose config` cannot be run here; report that validation limit explicitly.

## Final verification

- Run focused Graphiti adapter and config tests, then the backend test suite.
- Build the Vite static assets and inspect the production Docker/Compose/Nginx configuration. Docker and Compose are not installed in the Pod, so image builds and Compose parsing are unavailable here.
- Inspect `git diff` and `git status` from `/workspace/MiroFish`; verify all pre-existing dirty changes remain and no host or Kubernetes resources were changed.
- Do not claim the external VPN/firewall boundary was verified; cluster-wide read access was denied.
