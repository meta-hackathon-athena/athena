# frontend/ — reserved for the Vixy app

This directory is reserved for the mobile application (owner: Ara, Milestone 4). It intentionally
contains no code yet.

Contract with the backend:

- The app talks to the backend **only over HTTP** (`/v1`, JSON). It never imports `vixy`, `agent`,
  `memory`, `context_engine` or `prod_dreamer_model`.
- Keep the base URL in one setting, e.g. `EXPO_PUBLIC_VIXY_API=http://127.0.0.1:8765/v1`.
- Everything the app can call is documented, with real responses, in
  [docs/FRONTEND_INTEGRATION.md](../docs/FRONTEND_INTEGRATION.md). That includes polling for
  notifications, the simulator, and the CORS/auth limits.
- Start a backend with demo data from the repository root:

  ```bash
  make demo-data VIXY_USER=app_demo && make serve VIXY_USER=app_demo
  ```

The repository `.gitignore` already excludes `node_modules/`, `.expo/` and `.next/`. Never commit
`.env` files or API keys from the app either.
