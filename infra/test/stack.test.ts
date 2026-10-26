import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { RemoteMcpStack } from '../lib/remote-mcp-stack';

const template = Template.fromStack(
  new RemoteMcpStack(new App(), 'RemoteMcp', {
    env: { account: '111111111111', region: 'ap-southeast-2' },
    callbackPort: 8976,
    reservedConcurrency: 5,
    github: { owner: 'octo', repo: 'ai-remote-mcp', ownerId: '11', repoId: '22' },
    code: lambda.Code.fromInline('placeholder'),
    version: 'abc1234',
  }),
);

type Statement = { Action: string | string[]; Resource?: unknown; Condition?: unknown };

/** Every statement in the policies attached to the role whose logical id starts with `prefix`. */
function statementsOf(prefix: string): Statement[] {
  const roleIds = Object.keys(template.findResources('AWS::IAM::Role')).filter((id) => id.startsWith(prefix));
  return Object.values(template.findResources('AWS::IAM::Policy'))
    .filter((policy) => (policy.Properties.Roles as { Ref: string }[]).some((role) => roleIds.includes(role.Ref)))
    .flatMap((policy) => policy.Properties.PolicyDocument.Statement as Statement[]);
}

const actionsOf = (statements: Statement[]) => statements.flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
const serverStatements = () => statementsOf('ServerServiceRole');

test('the server runs behind Lambda Web Adapter on arm64', () => {
  template.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'ai-remote-mcp',
    Runtime: 'python3.13',
    Architectures: ['arm64'],
    Handler: 'run.sh',
    Timeout: 30,
    MemorySize: 512,
    ReservedConcurrentExecutions: 5,
    Layers: ['arn:aws:lambda:ap-southeast-2:753240598075:layer:LambdaAdapterLayerArm64:30'],
    Environment: {
      Variables: Match.objectLike({
        AWS_LAMBDA_EXEC_WRAPPER: '/opt/bootstrap',
        CONFIG_PREFIX: '/ai-remote-mcp/',
        SEARCH_FUNCTION: 'team-docs-search',
        SERVER_VERSION: 'abc1234',
      }),
    },
  });
});

test('the public URL points at the live alias, and versions are kept for rollback', () => {
  template.hasResourceProperties('AWS::Lambda::Alias', { Name: 'live' });
  template.hasResourceProperties('AWS::Lambda::Url', {
    AuthType: 'NONE',
    InvokeMode: 'BUFFERED',
    TargetFunctionArn: { 'Fn::GetAtt': [Match.stringLikeRegexp('^Server'), 'Arn'] },
    Qualifier: 'live',
    Cors: Match.absent(),
  });
  const versions = Object.values(template.findResources('AWS::Lambda::Version'));
  expect(versions.length).toBe(1);
  expect(versions[0].DeletionPolicy).toBe('Retain');
});

test('anyone may call the URL, as function URLs with auth NONE need since October 2025', () => {
  template.hasResourceProperties('AWS::Lambda::Permission', {
    Action: 'lambda:InvokeFunctionUrl',
    Principal: '*',
    FunctionUrlAuthType: 'NONE',
  });
  template.hasResourceProperties('AWS::Lambda::Permission', {
    Action: 'lambda:InvokeFunction',
    Principal: '*',
    InvokedViaFunctionUrl: true,
  });
});

test('the server role may invoke only the search function and read only its settings', () => {
  const statements = serverStatements();
  expect(actionsOf(statements).sort()).toEqual(['lambda:InvokeFunction', 'ssm:GetParameters']);
  const invoke = statements.find((s) => s.Action === 'lambda:InvokeFunction');
  expect(JSON.stringify(invoke?.Resource)).toContain(':function:team-docs-search');
  const read = statements.find((s) => s.Action === 'ssm:GetParameters');
  const params = (read?.Resource as unknown[]).map((r) => JSON.stringify(r));
  expect(params).toHaveLength(3);
  for (const name of ['server-url', 'user-pool-id', 'client-id']) {
    expect(params.some((p) => p.includes(`parameter/ai-remote-mcp/${name}`))).toBe(true);
  }
});

test('the logs are kept for 30 days', () => {
  template.hasResourceProperties('AWS::Logs::LogGroup', { LogGroupName: '/aws/lambda/ai-remote-mcp', RetentionInDays: 30 });
});

test('sign-in: Essentials, no self sign-up, managed login', () => {
  template.hasResourceProperties('AWS::Cognito::UserPool', {
    UserPoolTier: 'ESSENTIALS',
    AdminCreateUserConfig: { AllowAdminCreateUserOnly: true },
    MfaConfiguration: 'OFF',
  });
  template.hasResourceProperties('AWS::Cognito::UserPoolDomain', {
    Domain: Match.stringLikeRegexp('^ai-remote-mcp-[0-9a-f]{8}$'),
    ManagedLoginVersion: 2,
  });
  template.hasResourceProperties('AWS::Cognito::ManagedLoginBranding', { UseCognitoProvidedValues: true });
});

test('the domain prefix does not contain the account id', () => {
  const [domain] = Object.values(template.findResources('AWS::Cognito::UserPoolDomain'));
  expect(domain.Properties.Domain).not.toContain('111111111111');
});

test('Claude Code signs in as a public client with a fixed loopback port and rotated refresh tokens', () => {
  template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
    GenerateSecret: false,
    AllowedOAuthFlows: ['code'],
    AllowedOAuthFlowsUserPoolClient: true,
    CallbackURLs: ['http://localhost:8976/callback', 'http://127.0.0.1:8976/callback'],
    SupportedIdentityProviders: ['COGNITO'],
    RefreshTokenRotation: { Feature: 'ENABLED', RetryGracePeriodSeconds: 10 },
    ExplicitAuthFlows: ['ALLOW_USER_SRP_AUTH'],
    AccessTokenValidity: 60,
  });
});

test('the resource server identifier is the server URL, so the scope and aud name this server', () => {
  const [server] = Object.values(template.findResources('AWS::Cognito::UserPoolResourceServer'));
  const identifier = JSON.stringify(server.Properties.Identifier);
  expect(identifier).toContain('FunctionUrl');
  expect(identifier).toContain('mcp');
  expect(server.Properties.Scopes).toEqual([{ ScopeName: 'search', ScopeDescription: 'Search the team docs' }]);
  // Cognito allows only [\w\s+=,.@-] in the name; the first deploy failed when it was the URL.
  expect(server.Properties.Name).toMatch(/^[\w\s+=,.@-]+$/);
});

test('the server reads its settings from three SSM parameters', () => {
  for (const name of ['server-url', 'user-pool-id', 'client-id']) {
    template.hasResourceProperties('AWS::SSM::Parameter', { Name: `/ai-remote-mcp/${name}`, Type: 'String' });
  }
});

describe('GitHub Actions roles', () => {
  const roles = Object.entries(template.findResources('AWS::IAM::Role')).filter(([id]) => id.startsWith('GitHub'));

  test('both trust only the main branch of this repo, by GitHub ids, through the existing OIDC provider', () => {
    expect(roles.map(([id]) => id).sort()).toEqual([expect.stringMatching(/^GitHubDeploy/), expect.stringMatching(/^GitHubRollback/)]);
    template.resourceCountIs('AWS::IAM::OIDCProvider', 0);
    for (const [, role] of roles) {
      const [statement] = role.Properties.AssumeRolePolicyDocument.Statement;
      expect(statement.Action).toBe('sts:AssumeRoleWithWebIdentity');
      expect(JSON.stringify(statement.Principal)).toContain('oidc-provider/token.actions.githubusercontent.com');
      expect(statement.Condition).toEqual({
        StringEquals: {
          'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
          'token.actions.githubusercontent.com:sub': 'repo:octo@11/ai-remote-mcp@22:ref:refs/heads/main',
        },
      });
      expect(role.Properties.MaxSessionDuration).toBe(3600);
    }
  });

  test('the deploy role may only assume the CDK bootstrap roles', () => {
    const statements = statementsOf('GitHubDeploy');
    expect(actionsOf(statements)).toEqual(['sts:AssumeRole']);
    const targets = JSON.stringify(statements[0].Resource);
    for (const kind of ['deploy', 'file-publishing', 'lookup']) expect(targets).toContain(`cdk-hnb659fds-${kind}-role-111111111111-ap-southeast-2`);
    expect(targets).not.toContain('cfn-exec-role');
  });

  test('the rollback role may only read and move the alias and read the server URL', () => {
    const statements = statementsOf('GitHubRollback');
    expect(actionsOf(statements).sort()).toEqual(['lambda:GetAlias', 'lambda:ListVersionsByFunction', 'lambda:UpdateAlias', 'ssm:GetParameter']);
    expect(JSON.stringify(statements)).toContain('parameter/ai-remote-mcp/server-url');
  });
});
