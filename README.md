# mars-runtime-demo-app

Sample app a MARS agent deploys on DigitalOcean App Platform: notes in Postgres, answers from
DO Serverless Inference, and a `/proof` endpoint that shows both round-trips actually worked.

| route | what it does |
|---|---|
| `GET /healthz` | liveness only, no dependencies |
| `GET /notes`, `POST /notes {"body": "..."}` | list / add notes (Postgres) |
| `POST /ask {"question": "..."}` | answers from your latest 20 notes via Serverless Inference |
| `GET /proof` | writes a row and reads it back, runs a tiny completion; `verdict` PASS / FAIL / DID-NOT-RUN |
| `GET /` | dark-only web page for the above |

`/proof` answers 200 only on PASS (503 otherwise). A missing env var reads as `not-configured` →
`DID-NOT-RUN`, never as a failure; a DB or model error reads as `fail` → `FAIL`.

Env: `DATABASE_URL` (DB component binding), `MODEL_ACCESS_KEY` (app secret), `INFERENCE_MODEL`
(required, no default), optional `INFERENCE_URL`, `APP_COMMIT`.

Run locally: `pip install -r requirements.txt pytest && pytest -q` (Postgres legs need `TEST_DATABASE_URL`).
