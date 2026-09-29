// The py-contoso-analytics runtime as an Azure Container Apps session pool.
//
//   az deployment group create -g <resource-group> -f sandbox/pool.bicep `
//     -p environmentId=<ACA env id> pullIdentityId=<UAMI id> image=<acr>/contoso-runtime/py-contoso-analytics:1
//
// Three settings carry the security story, so read them before changing them:
//   * sessionNetworkConfiguration.status = EgressDisabled - model-written code
//     cannot reach the internet or Azure. This is a pool setting, not a default.
//   * managedIdentitySettings lifecycle = None - the identity pulls the image
//     and is never exposed to code running in the session.
//   * no mcpServerSettings - MCP cannot be enabled on a CustomContainer pool
//     today (findings.md, F1). The agent calls the pool's REST API from its tool layer.
param location string = resourceGroup().location
param poolName string = 'pool-contoso-runtime'
param environmentId string
param pullIdentityId string
param image string
param runtimeName string = 'py-contoso-analytics'
param readySessions int = 2

resource pool 'Microsoft.App/sessionPools@2025-10-02-preview' = {
  name: poolName
  location: location
  tags: {
    'contoso-runtime': runtimeName
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pullIdentityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    poolManagementType: 'Dynamic'
    containerType: 'CustomContainer'
    scaleConfiguration: {
      maxConcurrentSessions: 10
      readySessionInstances: readySessions
    }
    dynamicPoolConfiguration: {
      lifecycleConfiguration: {
        lifecycleType: 'Timed'
        cooldownPeriodInSeconds: 300
      }
    }
    customContainerTemplate: {
      containers: [
        {
          name: 'runtime'
          image: image
          env: [
            { name: 'CONTOSO_RUNTIME', value: runtimeName }
          ]
          resources: {
            cpu: 1
            memory: '2Gi'
          }
          probes: [
            { type: 'Liveness', httpGet: { path: '/health', port: 6000 }, failureThreshold: 4 }
            { type: 'Startup', httpGet: { path: '/health', port: 6000 }, failureThreshold: 30, periodSeconds: 2 }
          ]
        }
      ]
      ingress: {
        targetPort: 6000
      }
      registryCredentials: {
        server: split(image, '/')[0]
        identity: pullIdentityId
      }
    }
    managedIdentitySettings: [
      {
        identity: pullIdentityId
        lifecycle: 'None'
      }
    ]
    sessionNetworkConfiguration: {
      status: 'EgressDisabled'
    }
  }
}

output poolManagementEndpoint string = pool.properties.poolManagementEndpoint
output poolId string = pool.id
