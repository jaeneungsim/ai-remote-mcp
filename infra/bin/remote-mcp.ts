#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { RemoteMcpStack } from '../lib/remote-mcp-stack';

const app = new cdk.App();
const reserved = app.node.tryGetContext('reservedConcurrency');

new RemoteMcpStack(app, 'RemoteMcp', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'ap-southeast-2' },
  stackName: 'ai-remote-mcp',
  description: 'Remote MCP server for the team docs search: Cognito login, call logs, deploy and rollback',
  callbackPort: Number(app.node.tryGetContext('callbackPort') ?? 8976),
  reservedConcurrency: reserved === undefined ? undefined : Number(reserved),
  github: app.node.tryGetContext('github'),
});

cdk.Tags.of(app).add('project', 'ai-remote-mcp');
