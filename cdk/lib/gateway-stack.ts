import * as path from 'path';
import { CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as apigw from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpJwtAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface McpGatewayStackProps extends StackProps {
  /** OIDC issuer URL, e.g. https://example.okta.com/oauth2/default or a Cognito user pool URL. */
  readonly oidcIssuer: string;
  /** Expected `aud` claim(s) in the access token. */
  readonly oidcAudience: string[];
  /** How long a connected upstream token is kept before it must be reconnected. */
  readonly tokenTtlDays?: number;
}

/** The encryption context key that binds each ciphertext to one user. */
export const ENCRYPTION_CONTEXT_KEY = 'user_sub';

export class McpGatewayStack extends Stack {
  constructor(scope: Construct, id: string, props: McpGatewayStackProps) {
    super(scope, id, props);

    // Customer-managed key used only for envelope-encrypting per-user upstream tokens.
    const tokenKey = new kms.Key(this, 'TokenKey', {
      description: 'Encrypts per-user upstream API tokens for the MCP gateway',
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.RETAIN,
      alias: 'alias/mcp-gateway/user-tokens',
    });

    // Table-level encryption at rest is separate from the per-item KMS envelope above,
    // so the table never needs broad kms:Decrypt grants on the token key.
    const table = new dynamodb.Table(this, 'UserTokens', {
      partitionKey: { name: 'user_sub', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      timeToLiveAttribute: 'expires_at',
      removalPolicy: RemovalPolicy.RETAIN,
    });

    const code = lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'src'), {
      exclude: ['**/__pycache__', '**/*.pyc'],
    });
    const env = {
      TABLE_NAME: table.tableName,
      KEY_ID: tokenKey.keyArn,
      TOKEN_TTL_DAYS: String(props.tokenTtlDays ?? 90),
    };

    const makeFn = (name: string, handler: string, description: string) =>
      new lambda.Function(this, name, {
        runtime: lambda.Runtime.PYTHON_3_12,
        architecture: lambda.Architecture.ARM_64,
        handler,
        code,
        description,
        memorySize: 256,
        timeout: Duration.seconds(20),
        environment: env,
        logGroup: new logs.LogGroup(this, `${name}Logs`, {
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: RemovalPolicy.DESTROY,
        }),
      });

    const mcpFn = makeFn('McpHandler', 'gateway.mcp_handler.handler', 'MCP Streamable HTTP endpoint');
    const connectFn = makeFn('ConnectHandler', 'gateway.connect_handler.handler', 'Connect or revoke a user token');

    // Least privilege. Every KMS call must carry a user_sub encryption context.
    const requireContext = { Null: { [`kms:EncryptionContext:${ENCRYPTION_CONTEXT_KEY}`]: 'false' } };

    table.grant(mcpFn, 'dynamodb:GetItem');
    mcpFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'DecryptUserTokensOnly',
      actions: ['kms:Decrypt'],
      resources: [tokenKey.keyArn],
      conditions: requireContext,
    }));

    table.grant(connectFn, 'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:DeleteItem');
    connectFn.addToRolePolicy(new iam.PolicyStatement({
      sid: 'EncryptUserTokensOnly',
      actions: ['kms:Encrypt'],
      resources: [tokenKey.keyArn],
      conditions: requireContext,
    }));

    const authorizer = new HttpJwtAuthorizer('OidcAuthorizer', props.oidcIssuer, {
      jwtAudience: props.oidcAudience,
    });

    const api = new apigw.HttpApi(this, 'Api', {
      apiName: 'mcp-gateway',
      description: 'Multi-user MCP gateway with per-user upstream credentials',
      createDefaultStage: false,
      defaultAuthorizer: authorizer,
    });
    const stage = api.addStage('DefaultStage', {
      stageName: '$default',
      autoDeploy: true,
      throttle: { rateLimit: 20, burstLimit: 40 },
    });

    const mcpIntegration = new HttpLambdaIntegration('McpIntegration', mcpFn);
    const connectIntegration = new HttpLambdaIntegration('ConnectIntegration', connectFn);

    api.addRoutes({ path: '/mcp', methods: [apigw.HttpMethod.POST, apigw.HttpMethod.GET, apigw.HttpMethod.DELETE], integration: mcpIntegration });
    api.addRoutes({ path: '/connect', methods: [apigw.HttpMethod.POST, apigw.HttpMethod.DELETE], integration: connectIntegration });
    api.addRoutes({ path: '/connect/status', methods: [apigw.HttpMethod.GET], integration: connectIntegration });

    new CfnOutput(this, 'McpUrl', { value: `${stage.url}mcp` });
    new CfnOutput(this, 'ConnectUrl', { value: `${stage.url}connect` });
    new CfnOutput(this, 'TokenTableName', { value: table.tableName });
  }
}
