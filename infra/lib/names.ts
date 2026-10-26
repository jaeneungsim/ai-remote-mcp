/** Fixed names: the README, the rollback workflow and the log queries refer to them. */
export const NAMES = {
  function: 'ai-remote-mcp',
  alias: 'live',
  /** The search function that ai-team-docs deploys. This stack only invokes it. */
  searchFunction: 'team-docs-search',
  /** SSM parameters the server reads at start (see server/remote_mcp/config.py). */
  configPrefix: '/ai-remote-mcp/',
  scope: 'search',
} as const;

/** Lambda Web Adapter 1.1.0 (layer version 30), from the README of github.com/aws/aws-lambda-web-adapter. */
export const webAdapterLayer = (region: string) => `arn:aws:lambda:${region}:753240598075:layer:LambdaAdapterLayerArm64:30`;
