# Copy this file to env.ps1 (gitignored), fill in the <placeholders>, and
# dot-source it before running any script in this folder:
#
#     Copy-Item env.example.ps1 env.ps1
#     . .\env.ps1
#
# Nothing in here is a secret. The platform's API key lives in a Foundry
# connection and is read by the agent at call time.

# --- Foundry project and model -----------------------------------------------
# Project endpoint: Foundry portal > your project > Overview > Endpoint.
# Model: the name of a deployment in that project. Any model that supports tool
# calling through the Responses API works. This sample was run with the
# open-weight Kimi-K2.7-Code and, as a baseline, gpt-4.1-mini (README, "Choosing
# the model"). To compare models, deploy the same code again under a second
# AGENT_NAME with a different deployment name.
$env:FOUNDRY_PROJECT_ENDPOINT       = "https://<foundry-account>.services.ai.azure.com/api/projects/<project>"
$env:AZURE_AI_MODEL_DEPLOYMENT_NAME = "Kimi-K2.7-Code"
$env:AGENT_NAME                     = "coding-agent"

# --- Runtime registry: runtime name -> session pool endpoint -----------------
# A method that claims `py-contoso-analytics-server` is tested in the pool mapped
# to `py-contoso-analytics`. Both pools should be EgressDisabled (findings.md, F2).
#
#   py-contoso-analytics : the custom-container pool built from
#                     sandbox/runtime/py-contoso-analytics.json. Use its
#                     `poolManagementEndpoint` (an output of sandbox/pool.bicep).
#                     The regional dynamicsessions.io URL does NOT work for a
#                     custom-container pool (findings.md, F3).
#   py-generic      : a Microsoft-managed PythonLTS pool, for contrast. It uses
#                     the regional dynamicsessions.io URL.
$subscriptionId = "<subscription-id>"
$resourceGroup  = "<resource-group>"
$region         = "eastus2"
$pools = "https://$region.dynamicsessions.io/subscriptions/$subscriptionId/resourceGroups/$resourceGroup/sessionPools"
$env:SANDBOX_RUNTIMES = (@{
    "py-contoso-analytics" = "https://pool-contoso-runtime.<aca-environment-default-domain>"
    "py-generic"      = "$pools/pool-py-generic"
} | ConvertTo-Json -Compress)
$env:DEFAULT_RUNTIME = "py-contoso-analytics"

# --- The Contoso platform and the Foundry connection holding its key -------
# Platform URL: `az containerapp show -n contoso-backend -g <rg> --query properties.configuration.ingress.fqdn`
$env:CONTOSO_BACKEND_BASE       = "https://contoso-backend.<aca-environment-default-domain>"
$env:CONTOSO_BACKEND_CONNECTION = "contoso-backend"

# --- Foundry skills the agent may use (comma-separated allow-list) -----------
# The agent re-reads which of these exist on every request and loads the
# *default* version's body when it uses one, so publish and roll back need no
# redeploy.
$env:SKILL_NAMES = "anomaly-method-standard"

# --- Extra agent environment --------------------------------------------------
# Anything prefixed AGENTENV_ is forwarded into the agent container by
# deploy.py, with the prefix stripped.
# $env:AGENTENV_CONTOSO_TENANT_MODE = "multi"

# --- Evaluation (evaluation/README.md) ----------------------------------------
# The model deployment that judges the agent's traces. Use a different model
# family from the agent's model, so that no model grades itself.
$env:EVAL_JUDGE_DEPLOYMENT = "gpt-5.4"
