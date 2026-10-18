"""Stable IDs for the n8n credentials and workflows this repo manages.

Why fixed IDs: workflow JSON refers to credentials by ID, and re-importing with the same IDs
overwrites in place. Bootstrap, ``make use-local`` and ``make use-rds`` can therefore switch
what a credential points at without touching any workflow. n8n IDs are 16 characters.
"""

from typing import TypedDict


class CredentialRef(TypedDict):
    """The ``{id, name}`` object n8n stores in a node's ``credentials`` map."""

    id: str
    name: str


# Placeholder key (used as "@@credential:<key>" in n8n/src templates) -> credential reference.
CREDENTIALS: dict[str, CredentialRef] = {
    "app_postgres": {"id": "lnAppPostgres001", "name": "App DB (n8n_app)"},
    "gemini_api": {"id": "lnGeminiHeader01", "name": "Gemini API key"},
    "webhook_jwt": {"id": "lnWebhookJwt0001", "name": "Webhook JWT (HS256)"},
    "aws_ses": {"id": "lnAwsSesSender01", "name": "AWS SES (send-only IAM user)"},
}

# Template file stem -> workflow ID.
WORKFLOWS: dict[str, str] = {
    "a_product_discovery": "lnWfProductDisc1",
    "b_user_matching": "lnWfUserMatchin1",
    "c_notification": "lnWfNotificatio1",
    "llm_gateway": "lnWfLlmGateway01",
    "warm_match_cache": "lnWfMatchWarm001",
}

for _ref in CREDENTIALS.values():
    assert len(_ref["id"]) == 16, _ref
for _wf_id in WORKFLOWS.values():
    assert len(_wf_id) == 16, _wf_id
