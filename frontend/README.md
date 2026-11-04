# frontend/

- [`web/`](web/README.md): the Athena web app used for the demo video. It has two screens,
  **Clarify** (answer Athena's yes/no question) and **Memories** (memory cards from the knowledge
  graph), and no dependencies beyond Node.
- The mobile application (owner: Ara, Milestone 4) is still planned and would live next to it.

Contract with the backend, for every app in this directory:

- The app talks to the backend **only over HTTP** (`/v1`, JSON). It never imports `vixy`, `agent`,
  `memory`, `context_engine` or `prod_dreamer_model`.
- Keep the base URL in one setting, e.g. `EXPO_PUBLIC_VIXY_API=http://127.0.0.1:8765/v1`
  (the web app uses `ATHENA_API`).
- Everything the app can call is documented, with real responses, in
  [docs/FRONTEND_INTEGRATION.md](../docs/FRONTEND_INTEGRATION.md). That includes polling for
  notifications, answering questions, the simulator, and the CORS/auth limits.
- Start a backend with demo data from the repository root:

  ```bash
  make ui-demo VIXY_USER=app_demo && make serve VIXY_USER=app_demo
  ```

The repository `.gitignore` already excludes `node_modules/`, `.expo/` and `.next/`. Never commit
`.env` files or API keys from the app either.
