import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { chmodSync, copyFileSync, cpSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { AssetHashType } from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';

const ROOT = resolve(__dirname, '..', '..');
const SOURCES = ['server/remote_mcp', 'server/run.sh', 'requirements.txt'];
const SKIP = (path: string) => path.includes('__pycache__') || path.endsWith('.pyc');

function files(path: string): string[] {
  if (statSync(path).isFile()) return [path];
  return readdirSync(path).flatMap((name) => files(join(path, name)));
}

/** Hash only what goes into the bundle, so unrelated changes do not rebuild it. */
function sourceHash(): string {
  const hash = createHash('sha256');
  for (const file of SOURCES.flatMap((s) => files(join(ROOT, s))).filter((f) => !SKIP(f)).sort()) {
    hash.update(relative(ROOT, file)).update(readFileSync(file));
  }
  return hash.digest('hex');
}

/**
 * The server bundle: the remote_mcp package, run.sh (the handler Lambda Web Adapter starts) and the
 * pinned dependencies built for Linux arm64. Bundles locally with uv; falls back to Docker.
 */
export function serverCode(): lambda.Code {
  return lambda.Code.fromAsset(ROOT, {
    assetHashType: AssetHashType.CUSTOM,
    assetHash: sourceHash(),
    exclude: ['*', '!server', '!server/remote_mcp', '!server/remote_mcp/**', '!server/run.sh', '!requirements.txt', '**/__pycache__'],
    bundling: {
      image: lambda.Runtime.PYTHON_3_13.bundlingImage,
      platform: 'linux/arm64',
      command: [
        'bash', '-c',
        'pip install --no-cache-dir -r requirements.txt -t /asset-output && cp -r server/remote_mcp /asset-output/ '
          + '&& cp server/run.sh /asset-output/run.sh && chmod 755 /asset-output/run.sh',
      ],
      local: {
        tryBundle(outputDir: string): boolean {
          try {
            execFileSync('uv', [
              'pip', 'install', '--quiet', '--target', outputDir,
              '--python-platform', 'aarch64-manylinux_2_28', '--python-version', '3.13',
              '--only-binary', ':all:', '-r', join(ROOT, 'requirements.txt'),
            ], { stdio: 'inherit' });
            cpSync(join(ROOT, 'server', 'remote_mcp'), join(outputDir, 'remote_mcp'), { recursive: true, filter: (p) => !SKIP(p) });
            copyFileSync(join(ROOT, 'server', 'run.sh'), join(outputDir, 'run.sh'));
            chmodSync(join(outputDir, 'run.sh'), 0o755); // the handler must be executable
            return true;
          } catch (err) {
            process.stderr.write(`local bundling failed, trying Docker: ${err}\n`);
            return false;
          }
        },
      },
    },
  });
}

/** What decides the deployed server. A commit that touches none of these (docs, results, scripts) keeps the version. */
const DEPLOYED = ['server', 'infra', 'requirements.txt'];

/**
 * The last git commit that changed the deployed server, shown as the server version and in every
 * log line. A docs-only commit therefore deploys no new Lambda version. CI needs the full history.
 */
export function gitVersion(): string {
  try {
    const git = (...args: string[]) =>
      execFileSync('git', args, { cwd: ROOT, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
    const sha = git('log', '-1', '--format=%h', '--', ...DEPLOYED);
    const dirty = git('status', '--porcelain', '--untracked-files=no', '--', ...DEPLOYED);
    return sha ? (dirty ? `${sha}-dirty` : sha) : 'dev';
  } catch {
    return 'dev'; // not a git checkout
  }
}
