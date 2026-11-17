import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { App, AssetStaging, Stack } from '../lib';
import { IgnoreMode } from '../lib/fs';

/** An output directory is not a valid input to its own staged file asset. */
describe('asset staging under its own source', () => {
  let source: string;

  beforeEach(() => {
    source = fs.mkdtempSync(path.join(os.tmpdir(), 'cdk-staging-overlap-'));
    fs.writeFileSync(path.join(source, 'source.txt'), 'source content');
  });

  afterEach(() => {
    fs.rmSync(source, { recursive: true, force: true });
    AssetStaging.clearAssetHashCache();
  });

  const stage = (sourcePath: string, outdir: string, ignoreMode?: IgnoreMode, exclude?: string[]) => {
    const app = new App({ outdir });
    const stack = new Stack(app, 'Stack');
    return new AssetStaging(stack, 'Source', { sourcePath, ignoreMode, exclude });
  };

  test('an output below the source is not copied back into the source', () => {
    const staged = stage(source, path.join(source, 'cdk.out'));
    expect(fs.readdirSync(staged.absoluteStagedPath)).toEqual(['source.txt']);
    expect(fs.existsSync(path.join(staged.absoluteStagedPath, 'cdk.out'))).toBe(false);
  });

  test.each([IgnoreMode.GLOB, IgnoreMode.GIT, IgnoreMode.DOCKER])('%s handles negated user ignores without self-copy', (ignoreMode) => {
    const staged = stage(source, path.join(source, '.cdk', 'run-1'), ignoreMode, ['!**/.cdk/**']);
    expect(fs.readFileSync(path.join(staged.absoluteStagedPath, 'source.txt'), 'utf8')).toEqual('source content');
    expect(fs.existsSync(path.join(staged.absoluteStagedPath, '.cdk', 'run-1'))).toBe(false);
  });

  test('previous cloud assemblies do not affect the source hash or copy', () => {
    const previous = path.join(source, '.cdk', 'run-1');
    fs.mkdirSync(previous, { recursive: true });
    const manifest = path.join(previous, 'manifest.json');
    fs.writeFileSync(manifest, JSON.stringify({ version: 'old' }));

    const first = stage(source, path.join(source, '.cdk', 'run-2'));
    expect(fs.readdirSync(first.absoluteStagedPath)).toEqual(['source.txt']);
    fs.writeFileSync(manifest, JSON.stringify({ version: 'new' }));
    AssetStaging.clearAssetHashCache();
    const second = stage(source, path.join(source, '.cdk', 'run-3'));
    expect(second.assetHash).toEqual(first.assetHash);
    expect(fs.readdirSync(second.absoluteStagedPath)).toEqual(['source.txt']);
  });

  test('output outside source does not change the asset fingerprint', () => {
    const outdir = fs.mkdtempSync(path.join(os.tmpdir(), 'cdk-outside-overlap-'));
    try {
      const first = stage(source, outdir);
      const other = stage(source, path.join(source, '.cdk'));
      expect(first.assetHash).toEqual(other.assetHash);
    } finally {
      fs.rmSync(outdir, { recursive: true, force: true });
    }
  });

  test('source equal to the assembly output is rejected before recursive staging', () => {
    expect(() => stage(source, source)).toThrow(/must not be the cloud assembly output/);
  });
});
