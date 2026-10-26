import { createHash } from 'node:crypto';
import { ArnFormat, CfnOutput, Duration, RemovalPolicy, Stack, StackProps, Token } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { NAMES, webAdapterLayer } from './names';
import { gitVersion, serverCode } from './python-code';

export interface RemoteMcpStackProps extends StackProps {
  /** Claude Code listens here for the login redirect. Cognito matches callback URLs exactly, ports included. */
  callbackPort: number;
  /** Caps concurrent invocations: requests without a token still start the function. Unset: no cap. */
  reservedConcurrency?: number;
  /** The GitHub repo whose main branch may deploy and roll back. Unset: no CI roles. */
  github?: GitHubRepo;
  /** Tests pass inline code and a fixed version instead of bundling. */
  code?: lambda.Code;
  version?: string;
}

export interface GitHubRepo {
  owner: string;
  repo: string;
  /** GitHub's numeric ids. New repos get OIDC subjects like `repo:owner@1/name@2:ref:refs/heads/main`. */
  ownerId: string;
  repoId: string;
}

const GITHUB_OIDC = 'token.actions.githubusercontent.com';

/**
 * A remote MCP server for the ai-team-docs search:
 * Claude Code -> function URL (`live` alias) -> MCP server function -> team-docs-search.
 * Cognito signs people in; the server checks each access token itself.
 */
export class RemoteMcpStack extends Stack {
  constructor(scope: Construct, id: string, props: RemoteMcpStackProps) {
    super(scope, id, props);
    const version = props.version ?? gitVersion();

    const server = new lambda.Function(this, 'Server', {
      functionName: NAMES.function,
      description: 'Remote MCP server for the team docs search',
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      memorySize: 512,
      timeout: Duration.seconds(30),
      code: props.code ?? serverCode(),
      handler: 'run.sh',
      layers: [lambda.LayerVersion.fromLayerVersionArn(this, 'WebAdapter', webAdapterLayer(this.region))],
      environment: {
        AWS_LAMBDA_EXEC_WRAPPER: '/opt/bootstrap', // Lambda Web Adapter starts run.sh and proxies requests to it
        AWS_LWA_PORT: '8080',
        CONFIG_PREFIX: NAMES.configPrefix,
        SEARCH_FUNCTION: NAMES.searchFunction,
        SERVER_VERSION: version,
      },
      reservedConcurrentExecutions: props.reservedConcurrency,
      // Old versions stay so the `live` alias can be moved back to one (see the rollback workflow).
      currentVersionOptions: { removalPolicy: RemovalPolicy.RETAIN },
      logGroup: new logs.LogGroup(this, 'ServerLogs', {
        logGroupName: `/aws/lambda/${NAMES.function}`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
    });
    const live = new lambda.Alias(this, 'Live', { aliasName: NAMES.alias, version: server.currentVersion });
    // Auth type NONE: the server answers 401 with the metadata address itself, as the MCP auth spec asks.
    const url = live.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.NONE, invokeMode: lambda.InvokeMode.BUFFERED });
    const serverUrl = `${url.url}mcp`; // the function URL ends with a slash

    // The search function lives in the ai-team-docs stack. The server calls it with this role, never the user's token.
    server.addToRolePolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: [this.formatArn({ service: 'lambda', resource: 'function', resourceName: NAMES.searchFunction, arnFormat: ArnFormat.COLON_RESOURCE_NAME })],
    }));
    // ARNs from names, not from the parameters below: those depend on the function URL, which depends on this role.
    server.addToRolePolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameters'],
      resources: ['server-url', 'user-pool-id', 'client-id'].map((name) =>
        this.formatArn({ service: 'ssm', resource: 'parameter', resourceName: `${NAMES.configPrefix.slice(1)}${name}` })),
    }));

    const users = new cognito.UserPool(this, 'Users', {
      userPoolName: 'ai-remote-mcp',
      featurePlan: cognito.FeaturePlan.ESSENTIALS, // resource binding (aud) and refresh token rotation need it
      selfSignUpEnabled: false, // the one user is created by hand
      signInAliases: { username: true },
      accountRecovery: cognito.AccountRecovery.NONE, // no email or phone is kept; an admin resets a lost login
      // No MFA: the one user is the author and nothing is demoed (2026-10-26). Self sign-up stays off.
      mfa: cognito.Mfa.OFF,
      passwordPolicy: { minLength: 12 },
      removalPolicy: RemovalPolicy.DESTROY,
    });
    users.addDomain('Login', {
      cognitoDomain: { domainPrefix: domainPrefix(this.account) },
      // Resource binding, which puts the server URL into the token's `aud`, works only with managed login.
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });
    const search = new cognito.ResourceServerScope({ scopeName: NAMES.scope, scopeDescription: 'Search the team docs' });
    const resourceServer = users.addResourceServer('Mcp', {
      identifier: serverUrl,
      // The name defaults to the identifier, but names may not contain ':' or '/'.
      userPoolResourceServerName: 'team-docs-mcp',
      scopes: [search],
    });
    const client = users.addClient('ClaudeCode', {
      userPoolClientName: 'claude-code',
      generateSecret: false, // a public client: Claude Code holds no secret, and PKCE protects the code
      authFlows: { userSrp: true },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.resourceServer(resourceServer, search)],
        // Both forms: Claude Code has sent each of them in different versions.
        callbackUrls: [`http://localhost:${props.callbackPort}/callback`, `http://127.0.0.1:${props.callbackPort}/callback`],
      },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      refreshTokenValidity: Duration.days(30),
      // The MCP auth spec asks for rotated refresh tokens for public clients.
      refreshTokenRotationGracePeriod: Duration.seconds(10),
      preventUserExistenceErrors: true,
    });
    // Clients made through the API get no managed login style until one is created.
    new cognito.CfnManagedLoginBranding(this, 'LoginStyle', {
      userPoolId: users.userPoolId,
      clientId: client.userPoolClientId,
      useCognitoProvidedValues: true,
    });

    const settings: Record<string, string> = {
      'server-url': serverUrl,
      'user-pool-id': users.userPoolId,
      'client-id': client.userPoolClientId,
    };
    for (const [name, value] of Object.entries(settings)) {
      new ssm.StringParameter(this, `Setting-${name}`, { parameterName: `${NAMES.configPrefix}${name}`, stringValue: value });
    }

    if (props.github) this.addCiRoles(props.github, server);

    new CfnOutput(this, 'ServerUrl', { value: serverUrl, description: 'The url in .mcp.json' });
    new CfnOutput(this, 'ClientId', { value: client.userPoolClientId, description: 'oauth.clientId in .mcp.json' });
    new CfnOutput(this, 'Scope', { value: `${serverUrl}/${NAMES.scope}`, description: 'oauth.scopes in .mcp.json' });
    new CfnOutput(this, 'UserPoolId', { value: users.userPoolId });
    new CfnOutput(this, 'ServerVersion', { value: version });
  }

  /**
   * Two roles for GitHub Actions on the main branch, through the account's existing GitHub OIDC
   * provider. Deploy: may only assume the CDK bootstrap roles, which `cdk deploy` uses. Rollback:
   * may only read and move the `live` alias, and read the server URL for the check afterwards.
   */
  private addCiRoles(github: GitHubRepo, server: lambda.Function): void {
    const provider = this.formatArn({ service: 'iam', region: '', resource: 'oidc-provider', resourceName: GITHUB_OIDC });
    const fromMain = new iam.WebIdentityPrincipal(provider, {
      StringEquals: {
        [`${GITHUB_OIDC}:aud`]: 'sts.amazonaws.com',
        // With the ids, a renamed or re-created repo of the same name cannot assume the roles.
        [`${GITHUB_OIDC}:sub`]: `repo:${github.owner}@${github.ownerId}/${github.repo}@${github.repoId}:ref:refs/heads/main`,
      },
    });
    const deployer = new iam.Role(this, 'GitHubDeploy', {
      roleName: `${NAMES.function}-github-deploy`,
      description: 'GitHub Actions on main: deploys this stack through the CDK bootstrap roles',
      assumedBy: fromMain,
      maxSessionDuration: Duration.hours(1),
    });
    deployer.addToPolicy(new iam.PolicyStatement({
      actions: ['sts:AssumeRole'],
      resources: ['deploy', 'file-publishing', 'lookup'].map((kind) =>
        this.formatArn({ service: 'iam', region: '', resource: 'role', resourceName: `cdk-hnb659fds-${kind}-role-${this.account}-${this.region}` })),
    }));
    const rollback = new iam.Role(this, 'GitHubRollback', {
      roleName: `${NAMES.function}-github-rollback`,
      description: 'GitHub Actions on main: moves the live alias to another version, nothing else',
      assumedBy: fromMain,
      maxSessionDuration: Duration.hours(1),
    });
    rollback.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:GetAlias', 'lambda:UpdateAlias', 'lambda:ListVersionsByFunction'],
      resources: [server.functionArn, `${server.functionArn}:*`],
    }));
    rollback.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter'],
      resources: [this.formatArn({ service: 'ssm', resource: 'parameter', resourceName: `${NAMES.configPrefix.slice(1)}server-url` })],
    }));
    new CfnOutput(this, 'DeployRoleArn', { value: deployer.roleArn, description: 'Repo secret DEPLOY_ROLE_ARN' });
    new CfnOutput(this, 'RollbackRoleArn', { value: rollback.roleArn, description: 'Repo secret ROLLBACK_ROLE_ARN' });
  }
}

/** Cognito domain prefixes are unique per Region, so derive one from the account without showing its id. */
function domainPrefix(account: string): string {
  if (Token.isUnresolved(account)) throw new Error('Deploy with a concrete account (CDK_DEFAULT_ACCOUNT).');
  return `ai-remote-mcp-${createHash('sha256').update(account).digest('hex').slice(0, 8)}`;
}
