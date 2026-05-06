---
title: ADR-014 — LLM access via OCI Generative AI Inference
status: accepted
created: 2026-05-06
owner: architect
tags: [adr, llm, oci, openai, phase-1]
related: [ADR-001, ADR-012, DECISION-003, aira-comparison]
---

# ADR-014 — LLM access via OCI Generative AI Inference

## Status
Accepted (2026-05-06). Aligns app-side LLM calls with the AIRA-proven pattern (see [aira-comparison.md §1.3.1](../aira-comparison.md)) and with our in-DB embedding choice (ADR-012).

## Context
DECISION-003 chose OpenAI as the LLM and embedding provider. Originally we wired the application to call OpenAI's API directly via the `openai` Python SDK. AIRA's production system, however, uses OCI Generative AI Inference (`OCIGenAI`) as a credential-managed proxy to the same OpenAI models — the path used by `DBMS_VECTOR.UTL_TO_EMBEDDING` for in-DB embedding (ADR-012).

Three reasons to align the app-side path with that pattern:

1. **No OpenAI API key on app hosts.** Auth is via OCI Resource Principal (or user principal); the GenAI service handles upstream OpenAI credentialing. One fewer secret to rotate, one fewer auth surface.
2. **Compliance posture.** Data flows stay inside Oracle's perimeter; only OCI GenAI talks to OpenAI's egress.
3. **Matches AIRA exactly.** Same model id (`openai.text-embedding-3-large`, `openai.gpt-4o`); same cost ledger; same vendor relationship.

## Decision

### Primary path: OCI Generative AI Inference
The application's `LLMClient` calls **OCI GenAI Inference** by default, not OpenAI directly. Both `chat()` (parser/synthesizer/intent-classifier/eval-judge) and query-time `embed()` go through OCI GenAI.

OpenAI direct remains supported via a `mode: openai_direct` config switch — used only for local dev when OCI auth isn't available.

### Endpoints
OCI GenAI Inference endpoint per region (the user provides the one for their tenancy):
```
https://inference.generativeai.{region}.oci.oraclecloud.com
```
Examples seen in the AIRA doc and Oracle docs:
- `https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com`
- `https://inference.generativeai.us-chicago-1.oci.oraclecloud.com`
- `https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com`

Action paths used by the framework:
- `POST {endpoint}/20231130/actions/chat`        ← chat completions
- `POST {endpoint}/20231130/actions/embedText`   ← embeddings

### Model identifiers
The GenAI service prefixes proprietary models with `openai.`:

| Framework concept | OCI GenAI model id | Spec'd dim / context |
|---|---|---|
| Synthesis / parser / intent / eval-judge | `openai.gpt-4o` | 128k context |
| Embeddings | `openai.text-embedding-3-large` | 3072 dims |
| (future) reranker | TBD via Cohere on OCI | — |

These match what AIRA already uses in `DBMS_VECTOR.UTL_TO_EMBEDDING`. Model name is pinned at the schema level (per DECISION-003 / ADR-001) so a swap requires bumping `schema_version` and reindexing.

### Auth
- **OCI Compute / Functions:** `instance_principal` or `resource_principal` signing — no static creds needed.
- **Local dev:** `config_file` profile in `~/.oci/config`.
- **Fallback (mode: openai_direct):** OpenAI API key from OCI Vault as in the original design.

The `OCI_AUTH_METHOD` env var picks. `core/llm_oci.py` constructs the signer accordingly.

### Compartment ID
OCI GenAI Inference requires a `compartment_id` on every call. Sourced from `framework/config/{env}.yaml::vault.compartment_ocid` (the same compartment owning the rest of the framework's resources).

### Request/response shape
Different from OpenAI's API but mappable. The `LLMClient` shim exposes the *same* `chat()` and `embed()` surface to callers — the OCI-vs-direct switch is invisible above `core/llm.py`.

```python
# What every caller sees:
result = llm.chat(model="gpt-4o", messages=[...], temperature=0.0,
                  response_format={"type": "json_object"})
# OCI path translates internally:
#   model "gpt-4o" → serving_model_id "openai.gpt-4o"
#   messages list → oci.generative_ai_inference.models.Message[]
#   response_format → oci request body equivalent
```

### Cost telemetry
OCI GenAI returns token usage in its response (similar to OpenAI). Same `CostEvent` accounting as before; only the call path changes. Pricing table in `framework/eval/prices.yaml` continues to express prices per model — OCI GenAI bills against the same rate card as direct OpenAI for these models.

## Implementation

### Module layout
```
framework/core/
├── llm.py             # façade: factory + LLMClient Protocol
├── llm_oci.py         # OCI GenAI Inference impl  (DEFAULT)
└── llm_openai.py      # direct OpenAI impl  (fallback for local dev)
```

### Config
```yaml
# framework/config/adapters/llm.yaml  (new file)
provider: oci_genai          # oci_genai | openai_direct

oci_genai:
  endpoint: https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com
  compartment_ocid: ${vault.compartment_ocid}    # interpolated from env config
  auth: instance_principal                        # instance_principal | resource_principal | config_file
  config_profile: DEFAULT                          # only when auth == config_file
  models:
    chat: openai.gpt-4o                            # GenAI model id
    synthesis: openai.gpt-4o
    eval_judge: openai.gpt-4o
    embedding: openai.text-embedding-3-large
  retries: 3
  timeout_s: 60
  rate_limit:
    requests_per_minute: 600                       # tier-dependent

openai_direct:                                     # fallback mode only
  api_key_secret: vault://kb/openai-api-key
  org_id_secret: vault://kb/openai-org-id
  project_id_secret: vault://kb/openai-project-id
  models:
    chat: gpt-4o
    embedding: text-embedding-3-large
```

The framework also retains `framework/config/adapters/openai.yaml` for backward compat (it just delegates to `llm.yaml::openai_direct`).

### Auth at runtime
```python
import oci
from oci.generative_ai_inference import GenerativeAiInferenceClient

if cfg["auth"] == "instance_principal":
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    client = GenerativeAiInferenceClient(config={}, signer=signer,
                                         service_endpoint=cfg["endpoint"])
elif cfg["auth"] == "resource_principal":
    signer = oci.auth.signers.get_resource_principals_signer()
    client = GenerativeAiInferenceClient(config={}, signer=signer,
                                         service_endpoint=cfg["endpoint"])
else:  # config_file
    config = oci.config.from_file(profile_name=cfg.get("config_profile", "DEFAULT"))
    client = GenerativeAiInferenceClient(config=config,
                                         service_endpoint=cfg["endpoint"])
```

### Chat call
```python
from oci.generative_ai_inference.models import (
    ChatDetails, GenericChatRequest, OnDemandServingMode, Message, TextContent,
)

req = ChatDetails(
    compartment_id=self.compartment_ocid,
    serving_mode=OnDemandServingMode(model_id="openai.gpt-4o"),
    chat_request=GenericChatRequest(
        api_format="GENERIC",
        messages=[Message(role="USER", content=[TextContent(text=user_msg)])],
        max_tokens=max_tokens,
        temperature=temperature,
        is_stream=False,
        # JSON-mode equivalent:
        response_format={"type": "JSON_OBJECT"} if json_mode else None,
    ),
)
response = client.chat(req)
text = response.data.chat_response.choices[0].message.content[0].text
usage = response.data.chat_response.usage  # tokens_in / tokens_out
```

### Embed call
```python
from oci.generative_ai_inference.models import EmbedTextDetails, OnDemandServingMode

req = EmbedTextDetails(
    compartment_id=self.compartment_ocid,
    serving_mode=OnDemandServingMode(model_id="openai.text-embedding-3-large"),
    inputs=batch_of_texts,
)
response = client.embed_text(req)
vectors = response.data.embeddings  # list[list[float]] dim=3072
```

## Consequences

- **Removed app-side dependency on OpenAI Python SDK** (becomes optional, only used in `openai_direct` mode for local dev).
- **App hosts no longer hold OpenAI API keys.** Vault entries `openai-api-key` / `openai-org-id` / `openai-project-id` become optional (only present in environments using `openai_direct` mode).
- **OCI auth is the single source.** Same auth path as everything else (Vault, ADB, Object Storage) — operationally consistent.
- **Latency profile may differ slightly** from direct OpenAI. Real numbers come from Phase 1 eval; if material, we measure and document.
- **Eval CI cost cap unchanged** — same model, same per-token rate.
- **AIRA migration story tightens.** AIRA's existing `OCI_VECTOR_CRED` credential and our framework's GenAI client share the same compartment/endpoint config; one credential, two consumers.

## Considered alternatives

- **Direct OpenAI** (original ADR-001 design): rejected as the default. Functional, but adds an extra credential, an extra auth surface, and diverges from AIRA's pattern. Retained as `openai_direct` fallback.
- **OCI Generative AI Agents** (higher-level service): rejected for v1 — DECISION-001 already chose LangGraph for orchestration. Agents may be revisited if Phase 4 wants a managed alternative.
- **Mixed: OpenAI direct for chat, DBMS_VECTOR for embed:** rejected — inconsistent and adds a credential without clear benefit.

## Open questions for the user
1. **Region:** which OCI region's GenAI endpoint? (ashburn / chicago / frankfurt / phoenix / london / mumbai / tokyo / sydney). User to provide; default placeholder is us-ashburn-1.
2. **Model availability in your tenancy:** confirm `openai.gpt-4o` and `openai.text-embedding-3-large` are enabled. (The OCI GenAI service surfaces only models your tenancy has access to.)
3. **Rate-tier:** what RPM cap is provisioned? Set in `framework/config/adapters/llm.yaml::oci_genai.rate_limit.requests_per_minute`.

## References

- [aira-comparison.md §1.3.1](../aira-comparison.md) — the AIRA `OCIGenAI` pattern
- [ADR-001](ADR-001-tech-stack-baseline.md) — original tech-stack baseline
- [ADR-012](ADR-012-in-db-embedding.md) — in-DB embedding (uses the same provider)
- [DECISION-003](../../../pmo/decisions/DECISION-003-llm-provider.md) — OpenAI as the underlying model provider
- Oracle docs: [Generative AI Inference REST API](https://docs.oracle.com/en-us/iaas/api/#/en/generative-ai-inference/20231130/)
