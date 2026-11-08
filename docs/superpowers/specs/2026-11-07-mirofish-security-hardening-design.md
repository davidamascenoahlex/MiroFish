# MiroFish security hardening design

Date: 2026-11-07
Status: Approved by the user for implementation planning

## Goal

Harden the current self-hosted Graphiti worktree without replacing or discarding its existing uncommitted changes. Perform repository reads, edits, builds, and tests inside the Kubernetes workspace Pod only.

## Decisions

- Keep API authentication out of scope. The operator accepts the residual risk and intends to rely on the external VPN/firewall boundary. The current Kubernetes identity cannot list cluster-wide services or ingress resources, so the VPN restriction has not been independently verified and must not be represented as verified.
- Build the frontend as static production assets. Serve it through an Nginx proxy; route `/api` to the backend over the Pod-local interface. Expose only the proxy port through any NodePort; do not expose the Flask or Neo4j ports directly.
- Keep Neo4j dedicated to MiroFish. Restrict Graphiti reads and destructive graph operations to the requested `group_id` and Graphiti-owned records; add regression tests for cross-group access and deletion.
- Require authenticated TLS (`bolt+s://`) for remote Neo4j URIs. Permit plaintext Bolt only for loopback connections within the same Pod.
- Keep LLM and embedding credentials independent. The current configuration already requires a separate embedding key; preserve this invariant and its regression test.

## Scope

1. Replace the production Docker startup that launches Vite's development server with a production frontend build and static proxy configuration.
2. Tighten Graphiti entity lookups and graph deletion so an identifier from another group cannot expose or delete that group's records.
3. Validate Neo4j URI transport security according to the loopback/remote policy above.
4. Add focused tests and update directly related deployment documentation.

## Out of scope

- Adding application login, changing API authorization semantics, deploying MiroFish, creating a NodePort, or modifying firewall/VPN rules.
- Replacing the current worktree with a community fork or discarding existing user edits.
- Committing changes; no commit was requested.

## Validation

Run focused backend tests and production frontend/container build checks inside the workspace Pod. Review the final diff and confirm the pre-existing uncommitted Graphiti changes remain intact. Do not claim the external firewall/VPN restriction is verified by Kubernetes.
