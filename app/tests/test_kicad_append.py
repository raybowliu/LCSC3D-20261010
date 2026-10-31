"""KiCad append, project tables, model preservation and transactional failure tests."""
import copy
import faulthandler
from dataclasses import replace
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.core.app_logging import close_logging
from lcsc3d.core.errors import Cancelled, DownloadError
from lcsc3d.eda import library_files
from lcsc3d.eda.easyeda_import import parse_component
from lcsc3d.eda.kicad import FORMATS, build_library
from lcsc3d.eda.kicad_append import read_target, publish_files
from lcsc3d.eda.kicad_project import add_libraries_to_project, read_project
from lcsc3d.eda.kicad_sexpr import Document
from lcsc3d.services import backend, backups
from test_eda_ir import fixture
from test_export_modes import STEP, OBJ
from test_kicad import sexpr, child, children


class KiCadAppendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, LOCALAPPDATA=str(self.root / 'profile'))
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(close_logging)
        self.api = backend.NetworkApi(threading.Event())
        self.api.get_cad_data_of_component = lambda part: copy.deepcopy(fixture(part))
        self.api.get_step_3d_model = lambda uuid: STEP
        self.api.get_raw_3d_model_obj = lambda uuid: OBJ

    def targets(self, root, formats=FORMATS, *, models=False, mode='embedded'):
        options = backend.Options(root, formats, kicad_merge=True, kicad_symbol_name='符号',
                                  kicad_footprint_name='封装', bind_3d_models=models, kicad_3d_mode=mode)
        row = backend.download_part('C20197', options, self.api)
        self.assertEqual(row.status, '成功', row.message)
        return {key: str(root / name) for fmt, key, name in (
            ('KICAD_SYMBOL', 'kicad_symbol_target', '符号.kicad_sym'),
            ('KICAD_FOOTPRINT', 'kicad_footprint_target', '封装.pretty')) if fmt in formats}

    def project(self, root):
        path = root / '测试工程.kicad_pro'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"meta":{"version":1},"board":{"preserved":true}}\r\n')
        return path

    def options(self, targets=None, formats=FORMATS, **kwargs):
        return backend.Options(self.root / 'unused', formats, append_mode=True, export_target='kicad',
                               bind_3d_models=False, **(targets or {}), **kwargs)

    def snapshot(self, root):
        return {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}

    def test_existing_libraries_48_combinations_preserve_old_members_and_repeat_without_changes(self):
        choices = (('KICAD_SYMBOL',), ('KICAD_FOOTPRINT',), FORMATS)
        for index, (formats, models, keep, project_selected) in enumerate(itertools.product(
                choices, ((), ('STEP',), ('OBJ',), ('STEP', 'OBJ')), (False, True), (False, True))):
            with self.subTest(formats=formats, models=models, keep=keep, project=project_selected):
                root = self.root / str(index)
                targets = self.targets(root, formats)
                project = self.project(root / '工程') if project_selected else None
                options = self.options(targets, models + formats, keep_individual=keep,
                                       project_path=str(project) if project else '')
                old = self.snapshot(root)
                row = backend.download_part('C2040', options, self.api)
                self.assertEqual(row.status, '成功', row.message)
                self.assertEqual(set(row.completed_formats), set(models + formats))
                for key, value in targets.items():
                    if key == 'kicad_symbol_target':
                        document = Document(Path(value).read_bytes())
                        self.assertEqual(len(document.root.children('symbol')), 2)
                        old_document = Document(old[Path(value)])
                        self.assertIn(old_document.source(old_document.root.children('symbol')[0]), document.text)
                    else:
                        self.assertEqual(len(list(Path(value).glob('*.kicad_mod'))), 2)
                        for path, payload in old.items():
                            if path.is_relative_to(Path(value)):
                                self.assertEqual(path.read_bytes(), payload)
                if project:
                    self.assertEqual(project.read_bytes(), old[project])
                    self.assertFalse((project.parent / 'sym-lib-table').exists())
                    self.assertFalse((project.parent / 'fp-lib-table').exists())
                before_repeat = self.snapshot(root)
                repeated = backend.download_part('C2040', options, self.api)
                self.assertEqual(repeated.status, '成功', repeated.message)
                self.assertEqual(self.snapshot(root), before_repeat)

    def test_project_only_custom_directories_names_models_and_individual_output(self):
        for index, (formats, keep, models) in enumerate(itertools.product(
                (('KICAD_SYMBOL',), ('KICAD_FOOTPRINT',), FORMATS), (False, True), ((), ('STEP', 'OBJ')))):
            with self.subTest(formats=formats, keep=keep, models=models):
                root = self.root / str(index)
                project = self.project(root / '工程')
                original = project.read_bytes()
                options = self.options(formats=models + formats, project_path=str(project), keep_individual=keep,
                    project_kicad_symbol_directory=str(root / '符号目录'), project_kicad_symbol_name='测试符号.kicad_sym',
                    project_kicad_footprint_directory=str(root / '封装目录'), project_kicad_footprint_name='测试封装.pretty',
                    project_model_directory=str(root / '模型目录'), individual_directory=str(root / '独立器件'))
                for part in ('C20197', 'C2040'):
                    row = backend.download_part(part, options, self.api)
                    self.assertEqual(row.status, '成功', row.message)
                    self.assertEqual(project.read_bytes(), original)
                if 'KICAD_SYMBOL' in formats:
                    symbols = root / '符号目录/测试符号.kicad_sym'
                    self.assertEqual(len(children(sexpr(symbols.read_bytes()), 'symbol')), 2)
                    table = sexpr((project.parent / 'sym-lib-table').read_bytes())
                    self.assertEqual(len(children(table, 'lib')), 1)
                    self.assertEqual(child(child(table, 'lib'), 'name')[1], '测试符号')
                    if 'KICAD_FOOTPRINT' in formats:
                        for symbol in children(sexpr(symbols.read_bytes()), 'symbol'):
                            props = {item[1]: item[2] for item in children(symbol, 'property')}
                            nickname, name = props['Footprint'].split(':')
                            self.assertEqual(nickname, '测试封装')
                            self.assertTrue((root / '封装目录/测试封装.pretty' / (name + '.kicad_mod')).exists())
                if 'KICAD_FOOTPRINT' in formats:
                    self.assertEqual(len(list((root / '封装目录/测试封装.pretty').glob('*.kicad_mod'))), 2)
                    self.assertEqual(len(children(sexpr((project.parent / 'fp-lib-table').read_bytes()), 'lib')), 1)
                self.assertEqual(bool(list((root / '模型目录').rglob('*.step'))), bool(models))
                self.assertEqual(bool(list((root / '独立器件').rglob('*.kicad_sym'))), keep and 'KICAD_SYMBOL' in formats)

    def test_shared_footprint_reuse_name_collision_and_symbol_links(self):
        targets = self.targets(self.root / 'libs')
        original = next(Path(targets['kicad_footprint_target']).glob('*.kicad_mod'))
        before = original.read_bytes()
        first = parse_component(fixture('C20197'), 'C20198')
        changed = replace(first.footprint, items=first.footprint.items[:-1])
        second = replace(first, part='C20199', footprint=changed,
                         symbol=replace(first.symbol, name='Different'))
        from lcsc3d.eda.kicad_append import append_batch
        rows = [backend.Result(part, '等待合并', pending_formats=list(FORMATS)) for part in ('C20198', 'C20199')]
        entries = {row.part: (component, self.root / row.part, {}) for row, component in zip(rows, (first, second))}
        options = self.options(targets)
        append_batch(rows, entries, options, lambda: None,
                     {fmt: read_target(path, fmt) for fmt, path in options.merged_paths().items()}, {})
        self.assertEqual(original.read_bytes(), before)
        files = list(original.parent.glob('*.kicad_mod'))
        self.assertEqual(len(files), 2)
        self.assertTrue(any(path.stem.endswith('_2') for path in files))
        symbols = children(sexpr(Path(targets['kicad_symbol_target']).read_bytes()), 'symbol')
        new = next(node for node in symbols if node[1] == 'Different')
        reference = next(node[2] for node in children(new, 'property') if node[1] == 'Footprint')
        self.assertTrue(reference.endswith('_2'))

    def test_embedded_and_external_models_survive_and_repeated_footprints_reuse(self):
        for mode in ('embedded', 'external'):
            targets = self.targets(self.root / mode, models=True, mode=mode)
            before = self.snapshot(Path(targets['kicad_footprint_target']))
            options = self.options(targets)
            options.bind_3d_models = True
            options.kicad_3d_mode = mode
            row = backend.download_part('C20197', options, self.api)
            self.assertEqual(row.status, '成功', row.message)
            self.assertEqual(self.snapshot(Path(targets['kicad_footprint_target'])), before)
            row = backend.download_part('C2040', options, self.api)
            self.assertEqual(row.status, '成功', row.message)
            for path, payload in before.items():
                self.assertEqual(path.read_bytes(), payload)

    def test_concurrent_symbol_edit_and_new_footprint_are_preserved(self):
        for kind in ('symbol', 'footprint'):
            targets = self.targets(self.root / kind)
            original = self.api.get_cad_data_of_component
            path = Path(targets['kicad_symbol_target']) if kind == 'symbol' else Path(targets['kicad_footprint_target']) / 'external.txt'
            def changed(part):
                path.write_bytes(b'external edit')
                return fixture(part)
            self.api.get_cad_data_of_component = changed
            row = backend.download_part('C2040', self.options(targets), self.api)
            self.api.get_cad_data_of_component = original
            self.assertEqual(row.status, '失败', row.message)
            self.assertEqual(path.read_bytes(), b'external edit')
            self.assertEqual(len(list(Path(targets['kicad_footprint_target']).glob('*.kicad_mod'))), 1)

    def test_write_failure_rolls_back_footprints_and_preserves_symbol(self):
        targets = self.targets(self.root / 'libs')
        before = self.snapshot(self.root / 'libs')
        write = backend.atomic_write
        def fail(path, payload):
            if Path(path) == Path(targets['kicad_symbol_target']):
                raise PermissionError('injected write failure')
            return write(path, payload)
        with patch.object(backend, 'atomic_write', side_effect=fail):
            row = backend.download_part('C2040', self.options(targets), self.api)
        self.assertEqual(row.status, '失败', row.message)
        self.assertEqual(self.snapshot(self.root / 'libs'), before)

    def test_cancel_during_publication_reverts_models_libraries_and_project_tables(self):
        project = self.project(self.root / '工程')
        before = self.snapshot(project.parent)
        move = library_files._move_new
        def cancelled(source, target):
            move(source, target)
            self.api.cancelled.set()
        options = self.options(project_path=str(project))
        with patch.object(library_files, '_move_new', side_effect=cancelled):
            row = backend.download_part('C2040', options, self.api)
        self.assertEqual(row.status, '已取消', row.message)
        self.assertEqual(self.snapshot(project.parent), before)

    def test_projects_without_meta_import_existing_libraries_and_preserve_original_bytes(self):
        targets = self.targets(self.root / '库')
        before = self.snapshot(self.root / '库')
        for index, payload in enumerate((b'{}', b'{\n}\n', b'\xef\xbb\xbf {\r\n}\r\n',
                                         b'{"board":{},"custom":"preserved"}\r\n')):
            with self.subTest(payload=payload):
                project = self.project(self.root / str(index))
                project.write_bytes(payload)
                self.assertEqual(read_project(project), payload)
                options = self.options(targets, (), project_path=str(project))
                options.validate_targets()
                imported = backend.import_existing_libraries(options)
                self.assertEqual((imported['added'], imported['skipped']), (2, 0))
                self.assertEqual(project.read_bytes(), payload)
                for name, kind in (('sym-lib-table', 'sym_lib_table'), ('fp-lib-table', 'fp_lib_table')):
                    table = Document((project.parent / name).read_bytes(), kind)
                    self.assertEqual(len(table.root.children('lib')), 1)
                after = self.snapshot(project.parent)
                repeated = backend.import_existing_libraries(options)
                self.assertEqual((repeated['added'], repeated['skipped']), (0, 2))
                self.assertEqual(self.snapshot(project.parent), after)
                self.assertEqual(self.snapshot(self.root / '库'), before)

    def test_empty_project_only_downloads_register_selected_formats_and_preserve_project(self):
        for index, formats in enumerate((('KICAD_SYMBOL',), ('KICAD_FOOTPRINT',), FORMATS)):
            with self.subTest(formats=formats):
                project = self.project(self.root / str(index))
                project.write_bytes(b'{}')
                options = self.options(formats=formats, project_path=str(project))
                row = backend.download_part('C2040', options, self.api)
                self.assertEqual(row.status, '成功', row.message)
                self.assertEqual(set(row.completed_formats), set(formats))
                self.assertEqual(project.read_bytes(), b'{}')
                self.assertEqual((project.parent / 'sym-lib-table').is_file(), 'KICAD_SYMBOL' in formats)
                self.assertEqual((project.parent / 'fp-lib-table').is_file(), 'KICAD_FOOTPRINT' in formats)

    def test_invalid_project_json_or_metadata_never_changes_project_or_libraries(self):
        targets = self.targets(self.root / '库')
        before = self.snapshot(self.root / '库')
        payloads = (b'', b' ', b'{', b'[]', b'null', b'"project"', b'1', b'true', b'\xff{}',
                    b'{"meta":null}', b'{"meta":[]}', b'{"meta":"invalid"}', b'{"meta":1}',
                    b'{"meta":false}')
        for index, payload in enumerate(payloads):
            with self.subTest(payload=payload):
                project = self.project(self.root / str(index))
                project.write_bytes(payload)
                with self.assertRaisesRegex(DownloadError, '工程格式无法识别'):
                    backend.import_existing_libraries(self.options(targets, (), project_path=str(project)))
                self.assertEqual(self.snapshot(project.parent), {project: payload})
                self.assertEqual(self.snapshot(self.root / '库'), before)

    def test_project_validation_errors_are_logged_and_empty_project_import_can_recover(self):
        from lcsc3d.core import app_logging
        from lcsc3d.core.app_paths import data_directory
        log_dir = data_directory() / 'logs'
        app_logging.configure_logging(version='test', directory=log_dir)
        targets = self.targets(self.root / '库')
        project = self.project(self.root / '工程')
        project.write_bytes(b'[]')
        options = self.options(targets, (), project_path=str(project))
        with self.assertRaises(DownloadError):
            backend.import_existing_libraries(options)
        entries = [json.loads(line) for line in (log_dir / 'LCSC3D.log').read_text('utf-8').splitlines()]
        error = next(entry for entry in entries if entry['event'] == 'library.kicad_project_import.failed')
        self.assertEqual(error['error'], 'DownloadError')
        self.assertTrue(error['error_id'])
        self.assertTrue(any(frame['file'] == 'kicad_project.py' and frame['function'] == 'read_project'
                            for frame in error['frames']))
        project.write_bytes(b'{}')
        imported = backend.import_existing_libraries(options)
        self.assertEqual(imported['added'], 2)
        self.assertEqual(project.read_bytes(), b'{}')
        entries = [json.loads(line) for line in (log_dir / 'LCSC3D.log').read_text('utf-8').splitlines()]
        registered = next(entry for entry in entries if entry['event'] == 'library.kicad_project_registered')
        self.assertEqual((registered['added'], registered['tables']), (2, 2))

    def test_import_existing_tables_preserve_unknown_bytes_skip_duplicates_and_back_up(self):
        targets = self.targets(self.root / '库')
        project = self.project(self.root / '工程')
        table = project.parent / 'sym-lib-table'
        initial = b'\xef\xbb\xbf# comment ) (\r\n(sym_lib_table (version 7) (custom "preserved"))\r\n'
        table.write_bytes(initial)
        before = self.snapshot(self.root / '库')
        options = self.options(targets, (), project_path=str(project))
        imported = backend.import_existing_libraries(options)
        self.assertEqual((imported['added'], imported['skipped']), (2, 0))
        self.assertTrue(table.read_bytes().startswith(initial[:initial.index(b'(custom')]))
        self.assertIn(b'(custom "preserved")', table.read_bytes())
        after = self.snapshot(project.parent)
        imported = backend.import_existing_libraries(options)
        self.assertEqual((imported['added'], imported['skipped']), (0, 2))
        self.assertEqual(self.snapshot(project.parent), after)
        self.assertEqual(self.snapshot(self.root / '库'), before)
        row = next(row for row in backups.list_backups() if row.name == 'sym-lib-table')
        self.assertEqual(backups.verified_payload(row), initial)
        plan = backups.prepare_restore(row.id, table)
        backups.restore_backup(plan)
        self.assertEqual(table.read_bytes(), initial)

    def test_project_nickname_conflict_and_second_table_failure_never_partially_import(self):
        targets = self.targets(self.root / 'libs')
        project = self.project(self.root / '工程')
        table = project.parent / 'fp-lib-table'
        table.write_text('(fp_lib_table (lib (name "封装")(type "KiCad")(uri "other.pretty")))', 'utf-8')
        before = self.snapshot(project.parent)
        with self.assertRaises(DownloadError):
            add_libraries_to_project(project, list(targets.values()))
        self.assertEqual(self.snapshot(project.parent), before)
        table.unlink()
        before = self.snapshot(project.parent)
        move = library_files._move_new
        def fail(source, target):
            if Path(target).name == 'fp-lib-table':
                raise PermissionError('injected table failure')
            move(source, target)
        with patch.object(library_files, '_move_new', side_effect=fail), self.assertRaises(PermissionError):
            add_libraries_to_project(project, list(targets.values()))
        self.assertEqual(self.snapshot(project.parent), before)

    def test_invalid_libraries_and_documents_do_not_modify_user_files(self):
        for payload in (b'', b'(kicad_symbol_lib', b'(other)', b'(kicad_symbol_lib (version 1))',
                        b'(kicad_symbol_lib (version 20211014) (symbol "unclosed))'):
            path = self.root / 'invalid.kicad_sym'
            path.write_bytes(payload)
            with self.assertRaises(DownloadError):
                read_target(path, 'KICAD_SYMBOL')
            self.assertEqual(path.read_bytes(), payload)
        document = Document(b'(root (value "line\nwith (parentheses) and \\"quotes\\""))')
        self.assertEqual(document.root.children('value')[0].atom(1), 'line\nwith (parentheses) and "quotes"')

    def test_single_target_filters_unselected_format_and_model_directory_uses_symbol_first(self):
        targets = self.targets(self.root / '符号', ('KICAD_SYMBOL',))
        targets.update(self.targets(self.root / '封装', ('KICAD_FOOTPRINT',)))
        options = self.options(targets, ('STEP',))
        self.assertEqual(options.formats, ('STEP',) + FORMATS)
        self.assertEqual(options.model_folder(self.root / 'unused'), self.root / '符号/符号_3D')
        options = self.options({'kicad_footprint_target': targets['kicad_footprint_target']})
        self.assertEqual(options.formats, ('KICAD_FOOTPRINT',))
        self.assertEqual(set(options.merged_paths()), {'KICAD_FOOTPRINT'})

    def test_footprint_only_project_registration_failure_is_reported_as_partial(self):
        project = self.project(self.root / 'project')
        with patch('lcsc3d.eda.kicad_project.add_libraries_to_project', side_effect=PermissionError('table locked')):
            row = backend.download_part('C2040', self.options(formats=('KICAD_FOOTPRINT',), project_path=str(project)), self.api)
        self.assertEqual(row.status, '部分完成')
        self.assertIn('加入 PCB 工程失败', row.message)
        self.assertEqual(row.completed_formats, ['KICAD_FOOTPRINT'])

    def test_append_errors_have_diagnostics_and_logging_continues_after_clear(self):
        from lcsc3d.core import app_logging
        from lcsc3d.core.app_paths import data_directory
        from lcsc3d.services import log_support
        log_dir = data_directory() / 'logs'
        app_logging.configure_logging(version='test', directory=log_dir)
        app_logging.start_crash_capture()
        self.addCleanup(app_logging.stop_crash_capture)
        targets = self.targets(self.root / 'libs')
        write = backend.atomic_write
        def fail(path, payload):
            if Path(path) == Path(targets['kicad_symbol_target']):
                raise PermissionError('PRIVATE-write-failure')
            return write(path, payload)
        with patch.object(backend, 'atomic_write', side_effect=fail):
            row = backend.download_part('C2040', self.options(targets), self.api)
        self.assertEqual(row.status, '失败')
        entries = [json.loads(line) for line in (log_dir / 'LCSC3D.log').read_text('utf-8').splitlines()]
        error = next(entry for entry in entries if entry['event'] == 'download.kicad_failed')
        self.assertEqual((error['stage'], error['error']), ('append', 'PermissionError'))
        self.assertTrue(error['error_id'] and error['frames'])
        (data_directory() / 'store-session.bin').write_bytes(b'PRIVATE-test-session')
        bundle = log_support.package_logs()
        with zipfile.ZipFile(bundle['path']) as archive:
            self.assertIn('diagnostics.json', archive.namelist())
            self.assertNotIn(b'PRIVATE-test-session', b'\n'.join(archive.read(name) for name in archive.namelist()))
        log_support.clear_logs()
        self.assertTrue(app_logging.log_event('ERROR', 'kicad_append.after_clear'))
        self.assertIn('kicad_append.after_clear', (log_dir / 'LCSC3D.log').read_text('utf-8'))
        self.assertTrue(faulthandler.is_enabled())


if __name__ == '__main__':
    unittest.main()
