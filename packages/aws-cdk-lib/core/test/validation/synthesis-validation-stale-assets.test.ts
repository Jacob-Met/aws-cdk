import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { App, AssetStaging, CfnResource, Stack, Stage } from '../../lib';
import type { IPolicyValidationPluginBeta1 } from '../../lib';

/** The guard must protect deployed artifacts without traversing old asset staging directories. */
describe('validation plugin tamper snapshot uses the current assembly assets (#38614)', () => {
  let outdir: string;
  let source: string;
  let originalValidation: string | undefined;

  beforeEach(() => {
    outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'cdk-active-asset-guard-'));
    source = fs.mkdtempSync(path.join(os.tmpdir(), 'cdk-active-asset-source-'));
    fs.writeFileSync(path.join(source, 'content.txt'), 'original');
    originalValidation = process.env.CDK_VALIDATION;
    process.env.CDK_VALIDATION = 'false';
  });

  afterEach(() => {
    if (originalValidation === undefined) delete process.env.CDK_VALIDATION;
    else process.env.CDK_VALIDATION = originalValidation;
    fs.rmSync(outdir, { recursive: true, force: true });
    fs.rmSync(source, { recursive: true, force: true });
  });

  function synthWithPlugin(change: (active: string) => void, nested = false, docker = false) {
    let active = '';
    const plugin: IPolicyValidationPluginBeta1 = {
      name: 'asset-mutator',
      validate: () => {
        change(active);
        return { success: true, violations: [] };
      },
    };
    const app = new App({ outdir, autoSynth: false, confirmValidationPluginsDoNotMutate: true, policyValidationBeta1: [plugin] });
    const stage = nested ? new Stage(app, 'Nested') : app;
    const stack = new Stack(stage, 'Stack');
    new CfnResource(stack, 'Resource', { type: 'Test::Resource::Fake', properties: { value: 'original' } });
    const staging = new AssetStaging(stack, 'CurrentAsset', { sourcePath: source });
    if (docker) {
      stack.synthesizer.addDockerImageAsset({
        directoryName: staging.relativeStagedPath(stack),
        sourceHash: staging.assetHash,
      });
    } else {
      stack.synthesizer.addFileAsset({
        fileName: staging.relativeStagedPath(stack),
        sourceHash: staging.assetHash,
        packaging: staging.packaging,
      });
    }
    active = staging.absoluteStagedPath;
    return app;
  }

  test.each([false, true])('rejects tampering with current staged assets (nested stage: %s)', (nested) => {
    const app = synthWithPlugin(active => fs.writeFileSync(path.join(active, 'content.txt'), 'tampered'), nested);
    expect(() => app.synth()).toThrow(/asset-mutator.*modified the cloud assembly/);
  });

  test('rejects modification of a current Docker build context', () => {
    const app = synthWithPlugin(active => fs.writeFileSync(path.join(active, 'content.txt'), 'tampered'), false, true);
    expect(() => app.synth()).toThrow(/asset-mutator.*modified the cloud assembly/);
  });

  test('keeps nested asset-shaped directories protected inside a current deployable source', () => {
    const nested = `asset.${'b'.repeat(64)}`;
    fs.mkdirSync(path.join(source, nested));
    fs.writeFileSync(path.join(source, nested, 'nested.txt'), 'original');
    const app = synthWithPlugin(active => fs.writeFileSync(path.join(active, nested, 'nested.txt'), 'tampered'));
    expect(() => app.synth()).toThrow(/asset-mutator.*modified the cloud assembly/);
  });

  test('rejects removal of a currently deployed staged asset', () => {
    const app = synthWithPlugin(active => fs.unlinkSync(path.join(active, 'content.txt')));
    expect(() => app.synth()).toThrow(/asset-mutator.*modified the cloud assembly/);
  });

  test('rejects modification of non-asset assembly files', () => {
    const app = synthWithPlugin(() => fs.writeFileSync(path.join(outdir, 'manifest.json'), 'tampered'));
    expect(() => app.synth()).toThrow(/asset-mutator.*modified the cloud assembly/);
  });

  test('ignores stale content-addressed staging without bypassing current asset checks', () => {
    const stale = path.join(outdir, `asset.${'a'.repeat(64)}`);
    fs.mkdirSync(stale);
    fs.writeFileSync(path.join(stale, 'unused.txt'), 'old');
    const app = synthWithPlugin(() => fs.writeFileSync(path.join(stale, 'unused.txt'), 'changed'));
    expect(() => app.synth()).not.toThrow();
    expect(fs.readFileSync(path.join(stale, 'unused.txt'), 'utf8')).toBe('changed');
  });
});
