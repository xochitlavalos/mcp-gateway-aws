import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { McpGatewayStack } from '../lib/gateway-stack';

const ISSUER = 'https://idp.example.com/oauth2/default';
const AUDIENCE = 'api://mcp-gateway';

let template: Template;

beforeAll(() => {
  const app = new App();
  const stack = new McpGatewayStack(app, 'TestStack', {
    oidcIssuer: ISSUER,
    oidcAudience: [AUDIENCE],
    env: { account: '123456789012', region: 'us-east-1' },
  });
  template = Template.fromStack(stack);
});

type Statement = { Action: string | string[]; Condition?: Record<string, unknown>; Sid?: string };

function statementsFor(policyPrefix: string): Statement[] {
  const policies = template.findResources('AWS::IAM::Policy');
  const ids = Object.keys(policies).filter((id) => id.startsWith(policyPrefix));
  expect(ids).toHaveLength(1);
  return policies[ids[0]].Properties.PolicyDocument.Statement as Statement[];
}

const actionsOf = (s: Statement) => (Array.isArray(s.Action) ? s.Action : [s.Action]);

test('KMS key has rotation enabled', () => {
  template.hasResourceProperties('AWS::KMS::Key', { EnableKeyRotation: true });
});

test('DynamoDB table is encrypted, has PITR, and uses TTL', () => {
  template.hasResourceProperties('AWS::DynamoDB::Table', {
    SSESpecification: { SSEEnabled: true },
    PointInTimeRecoverySpecification: { PointInTimeRecoveryEnabled: true },
    TimeToLiveSpecification: { AttributeName: 'expires_at', Enabled: true },
    KeySchema: [{ AttributeName: 'user_sub', KeyType: 'HASH' }],
  });
});

test('JWT authorizer uses the configured issuer and audience', () => {
  template.hasResourceProperties('AWS::ApiGatewayV2::Authorizer', {
    AuthorizerType: 'JWT',
    JwtConfiguration: { Issuer: ISSUER, Audience: [AUDIENCE] },
    IdentitySource: ['$request.header.Authorization'],
  });
});

test('every route requires the JWT authorizer', () => {
  const routes = template.findResources('AWS::ApiGatewayV2::Route');
  const keys = Object.values(routes).map((r) => r.Properties.RouteKey).sort();
  expect(keys).toEqual(['DELETE /connect', 'DELETE /mcp', 'GET /connect/status', 'GET /mcp', 'POST /connect', 'POST /mcp']);
  for (const route of Object.values(routes)) {
    expect(route.Properties.AuthorizationType).toBe('JWT');
    expect(route.Properties.AuthorizerId).toBeDefined();
  }
});

test('stage is throttled', () => {
  template.hasResourceProperties('AWS::ApiGatewayV2::Stage', {
    StageName: '$default',
    DefaultRouteSettings: Match.objectLike({ ThrottlingRateLimit: 20, ThrottlingBurstLimit: 40 }),
  });
});

test('MCP handler can only decrypt, only with a user encryption context, and only read the table', () => {
  const statements = statementsFor('McpHandlerServiceRoleDefaultPolicy');
  const all = statements.flatMap(actionsOf);

  expect(all).toContain('kms:Decrypt');
  expect(all).not.toContain('kms:Encrypt');
  expect(all.filter((a) => a.startsWith('dynamodb:'))).toEqual(['dynamodb:GetItem']);

  const kmsStatements = statements.filter((s) => actionsOf(s).some((a) => a.startsWith('kms:')));
  for (const s of kmsStatements) {
    expect(s.Condition).toEqual({ Null: { 'kms:EncryptionContext:user_sub': 'false' } });
  }
});

test('connect handler can only encrypt, with a user encryption context', () => {
  const statements = statementsFor('ConnectHandlerServiceRoleDefaultPolicy');
  const all = statements.flatMap(actionsOf);

  expect(all).toContain('kms:Encrypt');
  expect(all).not.toContain('kms:Decrypt');
  expect(all.filter((a) => a.startsWith('dynamodb:')).sort()).toEqual(
    ['dynamodb:DeleteItem', 'dynamodb:GetItem', 'dynamodb:PutItem'],
  );

  const kmsStatements = statements.filter((s) => actionsOf(s).some((a) => a.startsWith('kms:')));
  for (const s of kmsStatements) {
    expect(s.Condition).toEqual({ Null: { 'kms:EncryptionContext:user_sub': 'false' } });
  }
});

test('no IAM statement grants wildcard actions', () => {
  const policies = template.findResources('AWS::IAM::Policy');
  for (const p of Object.values(policies)) {
    for (const s of p.Properties.PolicyDocument.Statement as Statement[]) {
      for (const a of actionsOf(s)) expect(a).not.toMatch(/\*$/);
    }
  }
});

test('Lambdas run Python 3.12', () => {
  const fns = template.findResources('AWS::Lambda::Function');
  expect(Object.keys(fns)).toHaveLength(2);
  for (const fn of Object.values(fns)) expect(fn.Properties.Runtime).toBe('python3.12');
});
