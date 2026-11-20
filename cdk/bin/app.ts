#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { McpGatewayStack } from '../lib/gateway-stack';

const app = new App();

const issuer = app.node.tryGetContext('oidcIssuer') as string | undefined;
const audience = app.node.tryGetContext('oidcAudience') as string | undefined;
if (!issuer || !audience) {
  throw new Error(
    'Set OIDC context, e.g. cdk deploy -c oidcIssuer=https://example.okta.com/oauth2/default -c oidcAudience=api://mcp-gateway',
  );
}

new McpGatewayStack(app, 'McpGatewayStack', {
  oidcIssuer: issuer,
  oidcAudience: audience.split(',').map((a) => a.trim()),
  tokenTtlDays: Number(app.node.tryGetContext('tokenTtlDays') ?? 90),
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION },
});
