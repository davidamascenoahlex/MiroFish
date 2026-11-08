# MiroFish self-hosted deployment design

Status: approved for implementation.

## Goals

- Run MiroFish without Zep Cloud or Ollama.
- Use OpenAI-compatible endpoints for chat and embeddings, with separate credentials.
- Keep checkout, edits, builds, and tests inside the Kubernetes workspace Pod and its persistent volume.

## Runtime shape

- A single persistent workload Pod will host the development workspace, MiroFish components, and any separately approved future application containers.
- The aggregate container resource requests and limits must stay within 8 CPU and 16 GiB. Resource allocation is shared; applications cannot be independently scheduled or scaled.
- One 20 GiB `ceph-block` PVC stores the checkout and persistent application data. It is shared storage with no per-application quota.
- MiroFish uses a static Nginx frontend/proxy. Any NodePort must target only that proxy; Flask binds to `127.0.0.1:5001`, and Neo4j Bolt must bind to `127.0.0.1:7687` inside the Pod.
- The operator chose to rely on the external VPN/firewall and not add application login. Kubernetes access in this session cannot verify the external restriction. Any client that can reach the proxy can operate on and delete MiroFish data.
- Docker Compose runs Nginx and Flask in separate containers; set `GUNICORN_BIND=0.0.0.0:5001` there and provide a certificate-verified remote `NEO4J_URI`. The Kubernetes same-Pod deployment uses loopback defaults.

## Operational tradeoffs

- Updating the Pod template or restarting the Pod interrupts every application hosted there. The PVC preserves files across Pod replacement.
- Containers in one Pod share its network namespace and resource budget; this is not equivalent to a VM or namespace-level isolation.
- Future applications are added only after their container, path route, resource allocation, and persistent-data needs are reviewed.
- Interrupted Graphiti builds cannot resume legacy Zep batch IDs. The API returns a recoverable `409` and retains the partial graph; an explicit `force=true` rebuild deletes it after request validation.

## Security and application requirements

- Build the frontend as static assets served by Nginx; do not run the Vite development server in production.
- Run Flask through Gunicorn with debug mode disabled.
- Route `/api/` through Nginx to the backend; do not expose the backend port directly via NodePort or a public Service.
- Fix simulation IPC so request-supplied identifiers cannot select another simulation's command channel or filesystem paths.
- Scope Graphiti/Neo4j operations to the active graph/group.
- Require independent chat and embedding credentials; do not fall back from one key to the other.
- Keep Neo4j and backend ports off the NodePort service. Permit plaintext only for direct `bolt://` connections to loopback in the same Pod. Reject unencrypted `neo4j://` routing because it can discover remote servers; use certificate-verified `bolt+s://` or `neo4j+s://` for remote Neo4j.


## Required application settings

- `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL_NAME` configure chat completions.
- `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL_NAME`, and `EMBEDDING_DIM` configure embeddings independently.
- `NEO4J_URI`, `NEO4J_USER`, and `NEO4J_PASSWORD` configure the local Neo4j store used by Graphiti.
- `SECRET_KEY` is required for Flask session signing. `GRAPHITI_STRUCTURED_OUTPUT_MODE` accepts `json_object` or `json_schema`.
- Never reuse the chat key as an implicit embedding fallback; inject secrets through Kubernetes Secrets rather than committing `.env` values.

## Languages and model compatibility

- Brazilian Portuguese (`pt`, shown as `Português (Brasil)`) is the default interface and model-response language. English and Chinese remain selectable, and existing browser language preferences are preserved.
- The initial Portuguese catalog covers the shared translation keys. Model-generated content and legacy hard-coded report labels are not a guarantee of fully translated output.
- Model settings are provided through backend environment variables or Kubernetes Secrets, not through a settings page in the frontend.
- An OpenAI-compatible endpoint is not a guarantee that every model works: chat must support Chat Completions, usable JSON output (including the configured Graphiti structured-output mode), and tool calls for OASIS agents. Provider-specific token and sampling parameters also need to work across all clients, not only the main chat wrapper.
- Embeddings require a separate embedding model/endpoint; `EMBEDDING_DIM` must match its output dimension. Validate an actual graph build and simulation with the selected provider before treating it as supported.
