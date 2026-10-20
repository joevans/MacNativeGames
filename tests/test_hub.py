"""The hub app (hub/hub.m, build_app.step_hub): one app with every game built into it. Synthetic bytes only, no game
files or keys."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import build_app

HUB_SOURCE = build_app.REPO / 'hub/hub.m'


def fake_tree(root, version):
    """A work tree with what step_hub copies, every file a few bytes."""
    tree = root / f'tree-{version}'
    files = [f'build/totk-native-{version}' if version != 'v0' else 'build/totk-native', 'build/pso_manifest.tsv',
             'build/pso_title.tsv', 'build/spirv-cross/spirv-cross', 'prebuilt/macos-arm64/totk_xlate', f'build/app-launcher-{version}',
             'build/hub', 'build/AppIcon.icns', f'extracted/{version}/romfs/romfs.bin',
             f'extracted/{version}/romfs/runtime-index.bin', f'build/shader-seed/{version}/pso_manifest.tsv',
             f'build/shader-seed/{version}/a_b.metal', f'build/shader-seed/{version}/b_b.metal'] + \
            [f'rewritten/{version}/{n}.patched.bin' for n in ('main', 'sdk', 'subsdk0')]
    for rel in files:
        p = tree / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(f'{version} {rel}'.encode())
    seed = tree / f'build/shader-seed/{version}'
    lib = tree / f'build/shader-libs/{build_app.sha256(seed / "a_b.metal")}.metallib'   # one of the two compiled ahead
    lib.parent.mkdir(parents=True, exist_ok=True); lib.write_bytes(b'lib')
    (seed / 'seed-manifest.json').write_text(json.dumps(dict(
        version=version, chain='totk-native-v7-unverified', shaders=8000,
        files={n: hashlib.sha256((seed / n).read_bytes()).hexdigest() for n in ('pso_manifest.tsv', 'a_b.metal', 'b_b.metal')})))
    return tree


def fake_data_bundle(tree, version, logs, *, bundle_id=build_app.BUNDLE_ID):
    """build_app.data_bundle without codesign: the image in a bundle of its own."""
    bundle = tree / f'build/data-bundles/GameData-{version}.bundle'
    build_app.clone(tree / f'extracted/{version}/romfs/romfs.bin', bundle / 'Contents/Resources/romfs.bin')
    return bundle


class StepHub(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.hub = self.root / 'MacNativeGames.app'
        self.logs = self.root / 'logs'

    def build(self, *versions):
        tree = self.root / 'tree'
        for version in versions:
            fake = fake_tree(self.root, version)
            subprocess.run(['rsync', '-a', f'{fake}/', f'{tree}/'], check=True)
        with patch.object(build_app, 'sign_app') as signing, patch.object(build_app, 'data_bundle', fake_data_bundle), \
             patch.object(build_app, 'app_seal_valid', return_value=True), \
             patch.object(build_app, 'remove_in_background', build_app.remove_tree):
            build_app.step_hub(tree, self.hub, self.logs, list(versions))
        signing.assert_called_once_with(self.hub.with_name(self.hub.name + '.partial'), self.logs)

    def test_each_version_is_a_game_and_earlier_games_stay(self):
        self.build('v0')
        self.build('v143')
        games = self.hub / 'Contents/Resources/games'
        self.assertEqual(sorted(p.name for p in games.iterdir()), ['totk-v0', 'totk-v143'])
        for ident, display, version in (('totk-v0', '1.0.0', 'v0'), ('totk-v143', '1.4.3', 'v143')):
            game = games / ident
            info = json.loads((game / 'game.json').read_text())
            self.assertEqual(info, dict(game='totk', title=build_app.APP_NAME, version=display, launcher='launcher', root='totk',
                                        icon='AppIcon.icns', shaders=dict(total=2, compiled=1),
                                        status=f'TOTK/shader-status-{version}.json'))
            self.assertEqual((game / 'launcher').read_bytes(), f'{version} build/app-launcher-{version}'.encode())
            self.assertTrue((game / 'AppIcon.icns').is_file())
            self.assertTrue((game / 'totk/build/totk-native').is_file())
            self.assertEqual([p.name for p in (game / 'totk/rewritten').iterdir()], [version])
            romfs = game / f'totk/extracted/{version}/romfs/romfs.bin'   # a link into the hub's signed data bundle
            self.assertTrue(romfs.is_symlink())
            self.assertEqual(romfs.resolve(), (self.hub / f'Contents/Helpers/GameData-{version}.bundle/Contents/Resources/romfs.bin').resolve())
            self.assertEqual(romfs.read_bytes(), f'{version} extracted/{version}/romfs/romfs.bin'.encode())
        info = plistlib.loads((self.hub / 'Contents/Info.plist').read_bytes())
        self.assertEqual((info['CFBundleIdentifier'], info['CFBundleExecutable']), (build_app.HUB_ID, build_app.HUB_NAME))
        self.assertTrue((self.hub / f'Contents/MacOS/{build_app.HUB_NAME}').is_file())
        self.assertFalse(self.hub.with_name(self.hub.name + '.partial').exists())

    def test_several_versions_in_one_build(self):
        self.build('v0', 'v143')
        self.assertEqual(sorted(p.name for p in (self.hub / 'Contents/Resources/games').iterdir()), ['totk-v0', 'totk-v143'])

    def test_same_version_is_replaced(self):
        self.build('v143')
        stale = self.hub / 'Contents/Resources/games/totk-v143/totk/stale.bin'; stale.write_bytes(b'old')
        self.build('v143')
        self.assertFalse(stale.exists())

    def test_failed_seal_keeps_the_previous_hub(self):
        self.build('v0')
        with patch.object(build_app, 'sign_app', side_effect=build_app.BuildError('seal rejected')), \
             patch.object(build_app, 'data_bundle', fake_data_bundle):
            with self.assertRaisesRegex(build_app.BuildError, 'seal rejected'):
                build_app.step_hub(fake_tree(self.root, 'v143'), self.hub, self.logs, ['v143'])
        self.assertEqual([p.name for p in (self.hub / 'Contents/Resources/games').iterdir()], ['totk-v0'])

    def test_hub_option_is_mac_only_and_not_with_out(self):
        game = self.root / 'game.xci'; game.write_bytes(b'x'); keys = self.root / 'prod.keys'; keys.write_text('')
        for extra in (['--hub', '-o', str(self.root / 'x.app')], ['--hub', '--platform', 'ios'], ['--all-versions'],
                      ['--hub', '--all-versions', '--update', str(game)], ['--hub', '--updates-dir', str(self.root)]):
            with self.subTest(extra=extra), self.assertRaises(build_app.BuildError):
                build_app.main([str(game), '-k', str(keys), *extra])


class FindUpdates(unittest.TestCase):
    """--all-versions: each NSP/NSZ next to the game is identified by its code; others are skipped."""
    def test_supported_updates_by_code_not_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = ['base.nsp', 'a.nsp', 'b.nsz', 'c.nsp', 'd.nsp', 'notes.txt']
            for n in names: (folder / n).write_bytes(b'x')
            kinds = {'a.nsp': 'v143', 'b.nsz': 'v112', 'c.nsp': None, 'd.nsp': 'v143'}   # c: unsupported; d: 1.4.3 again
            def identify(_tree, path, _keys, _logs):
                if kinds[path.name] is None: raise build_app.BuildError('unsupported update module hashes')
                return kinds[path.name], path.with_suffix('.container')
            said = []
            with patch.object(build_app, 'identify_update', side_effect=identify), \
                 patch.object(build_app, 'say', side_effect=said.append):
                found = build_app.find_updates(folder / 'tree', folder, folder / 'base.nsp', folder / 'keys', folder / 'logs')
            self.assertEqual(found, [('v112', folder / 'b.container'), ('v143', folder / 'a.container')])
            self.assertTrue(any('skipped c.nsp: unsupported' in s for s in said))
            self.assertTrue(any('skipped d.nsp: 1.4.3 again' in s for s in said))


class GamesFolder(unittest.TestCase):
    """build_app.py --hub with no game file: everything in the games folder, identified by verified code."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / 'games'; self.folder.mkdir()
        support = patch.object(build_app, 'SUPPORT', self.root / 'support'); support.start(); self.addCleanup(support.stop)

    def nsp(self, name, *inside):
        """A PFS0 header naming these files (and nothing after it)."""
        table = b''.join(n.encode() + b'\0' for n in inside)
        head = b'PFS0' + struct.pack('<III', len(inside), len(table), 0)
        entries = b''.join(struct.pack('<QQII', 0, 0, 0, 0) for _ in inside)
        (self.folder / name).write_bytes(head + entries + table)

    def test_scan_identifies_each_file_and_writes_the_index(self):
        self.nsp('base.nsp', 'a.nca')
        self.nsp('copy-of-base.nsp', 'b.nca')
        self.nsp('u143.nsp', '0100f2c0115b68000000000000000010.tik')
        self.nsp('u111.nsp', '0100f2c0115b68000000000000000010.tik')
        self.nsp('other.nsp', 'c.nca')
        (self.folder / 'prod.keys').write_text('')
        (self.folder / 'notes.txt').write_text('')
        bases = {'base.nsp', 'copy-of-base.nsp'}
        def identify_update(_tree, path, _keys, _logs):
            if path.name == 'u143.nsp': return 'v143', path
            raise build_app.BuildError('unsupported update module hashes')
        with patch.object(build_app, 'identify_base', side_effect=lambda _t, path, _k, _l: path.name in bases), \
             patch.object(build_app, 'identify_update', side_effect=identify_update), patch.object(build_app, 'say'):
            base, updates = build_app.scan_library(self.root / 'tree', self.folder, self.folder / 'prod.keys', self.root / 'logs')
        self.assertEqual(base, self.folder / 'base.nsp')
        self.assertEqual(updates, [('v0', None), ('v143', self.folder / 'u143.nsp')])
        index = json.loads((self.root / 'support/library-files.json').read_text())
        self.assertEqual(index['folder'], str(self.folder))
        rows = {r['name']: r for r in index['files']}
        self.assertNotIn('notes.txt', rows)
        self.assertEqual((rows['base.nsp']['kind'], rows['base.nsp']['version']), ('base', '1.0.0'))
        self.assertEqual(rows['copy-of-base.nsp']['kind'], 'unsupported')
        self.assertEqual((rows['u143.nsp']['kind'], rows['u143.nsp']['version'], rows['u143.nsp']['game']), ('update', '1.4.3', 'totk'))
        self.assertEqual(rows['u111.nsp']['note'], 'an update of a version this project does not support')
        self.assertEqual(rows['other.nsp']['note'], 'not a supported game file')
        self.assertEqual(rows['prod.keys']['kind'], 'keys')
        self.assertEqual(rows['base.nsp']['size'], (self.folder / 'base.nsp').stat().st_size)

    def test_only_one_file_builds_its_version_alone(self):
        self.nsp('base.nsp', 'a.nca')
        self.nsp('u143.nsp', '0100f2c0115b68000000000000000010.tik')
        self.nsp('other.nsp', 'c.nca')
        def identify_update(_tree, path, _keys, _logs):
            if path.name == 'u143.nsp': return 'v143', path
            raise build_app.BuildError('unsupported update module hashes')
        def scan(only):
            with patch.object(build_app, 'identify_base', side_effect=lambda _t, path, _k, _l: path.name == 'base.nsp'), \
                 patch.object(build_app, 'identify_update', side_effect=identify_update), patch.object(build_app, 'say'):
                return build_app.scan_library(self.root / 'tree', self.folder, self.folder / 'prod.keys', self.root / 'logs', only=only)
        self.assertEqual(scan('u143.nsp'), (self.folder / 'base.nsp', [('v143', self.folder / 'u143.nsp')]))
        self.assertEqual(scan('base.nsp'), (self.folder / 'base.nsp', [('v0', None)]))
        with self.assertRaisesRegex(build_app.BuildError, 'other.nsp: not a supported game file'):
            scan('other.nsp')
        with self.assertRaisesRegex(build_app.BuildError, 'no gone.nsp in'):
            scan('gone.nsp')

    def test_only_needs_the_games_folder(self):
        with self.assertRaisesRegex(build_app.BuildError, '--library and --only'):
            build_app.main([str(self.folder / 'base.nsp'), '-k', str(self.folder / 'prod.keys'), '--hub', '--only', 'base.nsp'])

    def test_no_base_game_is_an_error(self):
        with patch.object(build_app, 'say'), self.assertRaisesRegex(build_app.BuildError, 'no supported base game'):
            build_app.scan_library(self.root / 'tree', self.folder, self.folder / 'prod.keys', self.root / 'logs')

    def test_folder_is_remembered_and_keys_are_needed(self):
        with self.assertRaisesRegex(build_app.BuildError, 'no prod.keys in'):
            build_app.main(['--hub', '--library', str(self.folder)])
        self.assertEqual(json.loads((self.root / 'support/library.json').read_text())['folder'], str(self.folder.resolve()))

    def test_without_game_file_needs_hub_and_takes_every_version(self):
        for argv in ([], ['--hub', '--update', 'x.nsp'], ['--hub', '--all-versions'], ['x.nsp', '-k', 'k', '--library', 'f']):
            with self.subTest(argv=argv), self.assertRaises(build_app.BuildError):
                build_app.main(argv)


class WorkLock(unittest.TestCase):
    def test_second_build_in_the_same_work_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            holder = subprocess.Popen([sys.executable, '-c', 'import build_app,sys,pathlib,time; build_app.lock_work(pathlib.Path(sys.argv[1])); print("held", flush=True); time.sleep(30)', str(work)],
                                      stdout=subprocess.PIPE, text=True, cwd=build_app.REPO)
            try:
                self.assertEqual(holder.stdout.readline().strip(), 'held')
                with patch.dict(build_app._work_locks, clear=True), self.assertRaisesRegex(build_app.BuildError, 'another build'):
                    build_app.lock_work(work)
            finally:
                holder.kill(); holder.wait()


@unittest.skipUnless(subprocess.run(['xcrun', '--find', 'clang'], capture_output=True).returncode == 0, 'needs clang')
class HubCheck(unittest.TestCase):
    """hub --check runs each game's launcher --check with the game's folder and version."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.app = self.root / 'MacNativeGames.app'
        (self.app / 'Contents/MacOS').mkdir(parents=True)
        self.hub = self.app / 'Contents/MacOS/MacNativeGames'
        subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-O2', '-Wall', '-Werror', HUB_SOURCE, '-framework', 'AppKit',
                        '-framework', 'GameController', '-o', self.hub], check=True)
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps(dict(CFBundleExecutable='MacNativeGames',
                                                                           CFBundleIdentifier='org.macnativegames.hub.test')))

    def game(self, ident, version, ok=True):
        game = self.app / 'Contents/Resources/games' / ident
        (game / 'totk').mkdir(parents=True)
        (game / 'game.json').write_text(json.dumps(dict(title='Test', version=version, launcher='launcher', root='totk')))
        launcher = game / 'launcher'
        launcher.write_text(f'#!/bin/sh\necho "$1 $MNG_GAME_ROOT $MNG_GAME_VERSION" > "{game}/seen"\nexit {0 if ok else 1}\n')
        launcher.chmod(0o755)
        return game

    def test_every_launcher_is_checked(self):
        a, b = self.game('totk-v0', '1.0.0'), self.game('totk-v143', '1.4.3')
        r = subprocess.run([self.hub, '--check'], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((a / 'seen').read_text().split(), ['--check', str(a / "totk"), '1.0.0'])
        self.assertEqual((b / 'seen').read_text().split(), ['--check', str(b / "totk"), '1.4.3'])

    def test_one_failing_game_fails_the_check(self):
        self.game('totk-v0', '1.0.0'); self.game('totk-v143', '1.4.3', ok=False)
        r = subprocess.run([self.hub, '--check'], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('totk-v143 1.4.3: failed', r.stdout)

    def test_no_games_fails_the_check(self):
        r = subprocess.run([self.hub, '--check'], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)

    def test_watcher_stays_away_after_quit_all(self):
        quit_file = self.root / 'quit'; quit_file.write_text('')
        game = subprocess.Popen(['sleep', '0.3'])
        r = subprocess.run([self.hub, '--after', str(game.pid), quit_file], capture_output=True, text=True, timeout=30)
        game.wait()
        self.assertEqual(r.returncode, 0)
        self.assertFalse(quit_file.exists())   # used up; the hub was not opened

    def test_named_game_starts_without_the_hub(self):
        game = self.game('totk-v143', '1.4.3')
        r = subprocess.run([self.hub], env=dict(os.environ, MNG_GAME='totk-v143'), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)   # the launcher's own exit status: execv replaced the hub
        self.assertEqual((game / 'seen').read_text().split(), [str(game / "totk"), '1.4.3'])


if __name__ == '__main__':
    unittest.main()
