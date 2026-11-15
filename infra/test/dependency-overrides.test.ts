/**
 * The npm overrides in package.json keep jest's toolchain off minimatch below 11.0.1
 * (GHSA-g3hv-75wg-m249). Why they exist, the minimatch-only trap and when to drop them are
 * in infra/CLAUDE.md, "npm overrides".
 *
 * Nothing else in this suite loads those packages. Plain `npx jest` requires glob only for
 * `projects`, coverage-threshold globs or a module-not-found hint, and test-exclude only
 * under --coverage, so every other test passes whichever glob or minimatch is installed.
 * Without this file a Dependabot minor/patch PR, which dependabot-auto-merge.yml merges on
 * green CI, could put a vulnerable minimatch back on jest's side, or a glob that matches
 * nothing and raises nothing, and CI would stay green. So it pins both halves: the
 * lockfile holds no vulnerable copy outside aws-cdk-lib's bundle, and the copies jest
 * would load work the way jest calls them, on CI's Node.
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

const INFRA_ROOT = path.resolve(__dirname, '..');

// minimatch fixed GHSA-g3hv-75wg-m249 in 11.0.1. glob publishes an entry point with its own
// copy of minimatch built in, which the lockfile cannot show: 14.0.3 is the first glob whose
// copy has the fix (14.0.0 to 14.0.2 came out before minimatch 11.0.1 and still make the
// unbounded patterns), and it is past glob's own GHSA-3g3m-2fjr-6pw4 (fixed in 14.0.2).
const MINIMATCH_FLOOR = '11.0.1';
const GLOB_FLOOR = '14.0.3';

// aws-cdk-lib ships minimatch inside its own tarball (bundledDependencies), where no
// override reaches it; aws-cdk PR 39595 moves it to 11. That copy alone is exempt.
const CDK_BUNDLED_MINIMATCH = 'node_modules/aws-cdk-lib/node_modules/minimatch';

/** True when `version` sorts before `floor` (x.y.z; a prerelease sorts before its release). */
function versionBelow(version: string, floor: string): boolean {
  const parse = (v: string) => {
    const m = /^(\d+)\.(\d+)\.(\d+)(-[^+]+)?(?:\+.*)?$/.exec(v);
    if (!m) throw new Error(`not a semver version: ${v}`);
    return { parts: [Number(m[1]), Number(m[2]), Number(m[3])], prerelease: m[4] !== undefined };
  };
  const a = parse(version);
  const b = parse(floor);
  const first = a.parts.findIndex((n, i) => n !== b.parts[i]);
  return first === -1 ? a.prerelease && !b.prerelease : a.parts[first] < b.parts[first];
}

/** `version` itself when it is below `floor` (so a failure names it), otherwise null. */
function belowFloor(version: string, floor: string): string | null {
  return versionBelow(version, floor) ? version : null;
}

describe('versionBelow', () => {
  test('orders x.y.z numerically and puts a prerelease before its release', () => {
    expect(versionBelow('10.2.6', '11.0.1')).toBe(true);
    expect(versionBelow('9.0.9', '11.0.1')).toBe(true);
    expect(versionBelow('11.0.0', '11.0.1')).toBe(true);
    expect(versionBelow('11.0.1-rc.1', '11.0.1')).toBe(true);
    expect(versionBelow('11.0.1', '11.0.1')).toBe(false);
    expect(versionBelow('11.0.10', '11.0.9')).toBe(false);
    expect(versionBelow('14.0.6', '14.0.3')).toBe(false);
    expect(() => versionBelow('latest', '11.0.1')).toThrow('not a semver version');
  });
});

type LockEntry = { version?: string; inBundle?: boolean };

const LOCK_PACKAGES = (
  JSON.parse(fs.readFileSync(path.join(INFRA_ROOT, 'package-lock.json'), 'utf8')) as {
    packages: Record<string, LockEntry>;
  }
).packages;

/** Each lockfile copy of `name` below `floor`, as [install path, entry]. */
function lockedBelow(name: string, floor: string): Array<[string, LockEntry]> {
  return Object.entries(LOCK_PACKAGES).filter(
    ([key, entry]) =>
      (key === `node_modules/${name}` || key.endsWith(`/node_modules/${name}`)) &&
      versionBelow(entry.version ?? '0.0.0', floor),
  );
}

const named = (copies: Array<[string, LockEntry]>) => copies.map(([key, entry]) => `${key}@${entry.version}`);

describe('package-lock.json', () => {
  test(`holds no glob below ${GLOB_FLOOR}`, () => {
    expect(named(lockedBelow('glob', GLOB_FLOOR))).toEqual([]);
  });

  test(`holds no minimatch below ${MINIMATCH_FLOOR} but the copy aws-cdk-lib bundles`, () => {
    const unreachable = ([key, entry]: [string, LockEntry]) => key === CDK_BUNDLED_MINIMATCH && entry.inBundle === true;
    expect(named(lockedBelow('minimatch', MINIMATCH_FLOOR).filter((copy) => !unreachable(copy)))).toEqual([]);
  });
});

/**
 * The directory of the last package in `chain`, each found from the one before it the way
 * Node resolves a require() inside that package, starting at infra/.
 */
function installedDir(chain: readonly string[]): string {
  return chain.reduce(
    (from, name) => path.dirname(require.resolve(`${name}/package.json`, { paths: [from] })),
    INFRA_ROOT,
  );
}

function installedVersion(dir: string): string {
  return (JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8')) as { version: string }).version;
}

/** Every file under `dir`, recursively, leaving out dot entries as glob does by default. */
function filesUnder(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    if (entry.name.startsWith('.')) return [];
    const full = path.join(dir, entry.name);
    return entry.isDirectory() ? filesUnder(full) : [full];
  });
}

const sorted = (paths: string[]) => paths.map((p) => path.resolve(p)).sort();

// GHSA-g3hv-75wg-m249's bounded reproduction from minimatch issue 333: six `**/../a/b`
// portions at optimizationLevel 2. Unfixed code makes 64 patterns whatever braceExpandMax
// says; fixed code stops at braceExpandMax (as 11.0.1 does) or refuses with a RangeError
// (as 11.0.8 does).
const AMPLIFIER = Array(6).fill('**/../a/b').join('/');
const BOUNDED = { optimizationLevel: 2, braceExpandMax: 8 };

/** How many patterns `compile` makes; a refusal (RangeError) makes none. */
function patternsMade(compile: () => readonly unknown[]): number {
  try {
    return compile().length;
  } catch (err) {
    if ((err as Error).name === 'RangeError') return 0;
    throw err;
  }
}

type GlobModule = {
  glob: { sync(pattern: string, options: { windowsPathsNoEscape: boolean }): string[] };
  Glob: new (pattern: string, options: object) => { patterns: readonly unknown[] };
};
type MinimatchModule = { Minimatch: new (pattern: string, options: object) => { set: readonly unknown[] } };
type TestExcludeClass = new (options: object) => { shouldInstrument(filename: string): boolean };

// Where jest loads them from. jest calls glob.sync in jest-config (`projects`), jest-runtime
// (the module-not-found hint) and @jest/reporters (coverage-threshold globs). Under
// --coverage, @jest/transform hands each file to babel-plugin-istanbul, which asks
// test-exclude, which loads glob and minimatch.
const JEST = ['jest', '@jest/core'];
const TEST_EXCLUDE = [...JEST, 'jest-runtime', '@jest/transform', 'babel-plugin-istanbul', 'test-exclude'];
const GLOB_CHAINS = [
  [...JEST, 'jest-config', 'glob'],
  [...JEST, 'jest-runtime', 'glob'],
  [...JEST, '@jest/reporters', 'glob'],
  [...TEST_EXCLUDE, 'glob'],
];

for (const chain of GLOB_CHAINS) {
  describe(`glob via ${chain.slice(0, -1).join(' > ')}`, () => {
    const dir = installedDir(chain);
    const { glob, Glob } = require(dir) as GlobModule;

    test(`is ${GLOB_FLOOR} or later`, () => {
      expect(belowFloor(installedVersion(dir), GLOB_FLOOR)).toBeNull();
    });

    test('finds what the file system holds, called the way jest calls it', () => {
      const lib = path.join(INFRA_ROOT, 'lib');
      const options = { windowsPathsNoEscape: true };
      // jest-runtime's hint: `${pathToModule}.*`.
      expect(sorted(glob.sync(`${path.join(lib, 'env-config')}.*`, options))).toEqual(
        sorted(filesUnder(lib).filter((f) => path.basename(f).startsWith('env-config.'))),
      );
      // An absolute pattern with braces and a globstar, as `projects` and threshold groups can be.
      const tsUnderBinAndTest = [...filesUnder(path.join(INFRA_ROOT, 'bin')), ...filesUnder(path.join(INFRA_ROOT, 'test'))];
      expect(sorted(glob.sync(path.join(INFRA_ROOT, '{bin,test}', '**', '*.ts'), options))).toEqual(
        sorted(tsUnderBinAndTest.filter((f) => f.endsWith('.ts'))),
      );
    });

    test('compiles patterns with a minimatch that has the GHSA-g3hv-75wg-m249 fix', () => {
      expect(patternsMade(() => new Glob(AMPLIFIER, { ...BOUNDED, cwd: INFRA_ROOT }).patterns)).toBeLessThanOrEqual(
        BOUNDED.braceExpandMax,
      );
    });
  });
}

describe(`test-exclude via ${TEST_EXCLUDE.slice(0, -1).join(' > ')}`, () => {
  const dir = installedDir(TEST_EXCLUDE);
  const minimatchDir = installedDir([...TEST_EXCLUDE, 'minimatch']);

  test(`loads minimatch ${MINIMATCH_FLOOR} or later, which bounds the amplification`, () => {
    expect(belowFloor(installedVersion(minimatchDir), MINIMATCH_FLOOR)).toBeNull();
    const { Minimatch } = require(minimatchDir) as MinimatchModule;
    expect(patternsMade(() => new Minimatch(AMPLIFIER, BOUNDED).set)).toBeLessThanOrEqual(BOUNDED.braceExpandMax);
  });

  test('instruments infra sources but not node_modules or files outside infra/, given the options jest passes', () => {
    const TestExclude = require(dir) as TestExcludeClass;
    // What babel-plugin-istanbul builds from the options @jest/transform gives it: cwd is
    // rootDir, exclude [] and extension false; include and excludeNodeModules keep the
    // @istanbuljs/schema defaults ([] and true).
    const exclude = new TestExclude({ cwd: INFRA_ROOT, include: [], exclude: [], extension: false, excludeNodeModules: true });
    expect(exclude.shouldInstrument(path.join(INFRA_ROOT, 'lib', 'env-config.ts'))).toBe(true);
    expect(exclude.shouldInstrument(path.join(INFRA_ROOT, 'node_modules', 'aws-cdk-lib', 'core', 'lib', 'stack.js'))).toBe(false);
    expect(exclude.shouldInstrument(path.join(path.dirname(INFRA_ROOT), 'app', 'main.py'))).toBe(false);
  });
});
