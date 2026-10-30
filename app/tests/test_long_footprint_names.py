"""Keep PCB display names independent of the 31-character CFB directory limit."""
import copy
import io
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import olefile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lcsc3d.core import app_logging as logs
from lcsc3d.eda.altium import export_pcblib, MergedLibrary
from lcsc3d.eda.library_merge import LibraryIndex
from lcsc3d.services import backend
from altium_inspect import pcb, schematic, named_pcb_section, Reader, parameters


def fixture(part):
    return json.loads((Path(__file__).parent / 'fixtures' / (part + '.json')).read_text(encoding='utf-8'))


@unittest.skipUnless(sys.platform == 'win32', 'Native AD compound storage')
class LongFootprintNamesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'profile')})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(logs.close_logging)
        self.parts = ['C20197', 'C2040']
        self.data = {part: fixture(part) for part in self.parts}
        self.api = backend.NetworkApi(use_cache=False)
        self.api.get_cad_data_of_component = lambda part: copy.deepcopy(self.data[part])

    def names(self, payload):
        index = LibraryIndex(payload, 'PCBLIB')
        try:
            return list(index.names)
        finally:
            index.ole.close()

    def assert_footprint(self, payload, name, original):
        section = named_pcb_section(payload, name)
        self.assertLessEqual(len(section.encode('utf-16le')), 62)
        actual, pads, primitives = pcb(payload, section)
        self.assertEqual(actual, name)
        self.assertEqual((pads, primitives), pcb(original)[1:])
        with olefile.OleFileIO(io.BytesIO(payload)) as ole:
            values = parameters(Reader(ole.openstream([section, 'Parameters']).read()).block()[1])
            self.assertEqual(values['PATTERN'], name)

    def test_merged_library_preserves_a_long_name(self):
        name = 'CONNECTOR_SMD_P2.54_2X20_' + 'A' * 30 + '_RIGHT_ANGLE'
        self.data['C20197']['packageDetail']['title'] = name
        library = MergedLibrary('PCBLIB')
        library.add(self.data['C20197'], 'C20197', lambda: None)
        payload = library.build(['C20197'], lambda: None)
        self.assertEqual(self.names(payload), [name])
        self.assert_footprint(payload, name, export_pcblib(self.data['C20197'], 'C20197'))

    def test_long_common_prefixes_survive_merge_append_and_individual_copies(self):
        prefix = 'CONNECTOR_SMD_' + 'A' * 35
        names = {part: prefix + '_' + tail for part, tail in zip(self.parts, ('LEFT_ANGLE', 'RIGHT_ANGLE'))}
        for part, name in names.items():
            self.data[part]['packageDetail']['title'] = name
        originals = {part: export_pcblib(self.data[part], part) for part in self.parts}
        cases = [('individual', False, False)] + list(itertools.product(('merge', 'append'), (False, True), (False, True)))
        for mode, merge_sch, keep in cases:
            with self.subTest(mode=mode, merge_sch=merge_sch, keep=keep):
                root = self.root / f'{mode}-{merge_sch}-{keep}'
                root.mkdir()
                target = root / 'existing.PcbLib'
                previous = None
                if mode == 'append':
                    old = fixture('C2765186')
                    old['packageDetail']['title'] = prefix + '_ORIGINAL'
                    previous = export_pcblib(old, 'C2765186')
                    target.write_bytes(previous)
                combined = mode != 'individual'
                options = backend.Options(root, ('SCHLIB', 'PCBLIB'), merge_sch and combined, combined,
                    'symbols', 'footprints', keep, keep, bind_3d_models=False,
                    pcblib_target=str(target) if mode == 'append' else '')
                rows = backend.download_batch(self.parts, options, api=self.api, workers=1)
                self.assertEqual([row.status for row in rows], ['成功', '成功'])
                merged = options.merged_paths()
                for row in rows:
                    for file in row.files:
                        path = Path(file)
                        if path.suffix == '.SchLib':
                            section = row.part + '_Symbol' if path == merged.get('SCHLIB') else None
                            _, records, _ = schematic(path.read_bytes(), section)
                            self.assertEqual(next(r['MODELNAME'] for r in records if r.get('RECORD') == '45'), names[row.part])
                            self.assertEqual(next(r['TEXT'] for r in records if r.get('NAME') == 'Footprint'), names[row.part])
                        elif path.suffix == '.PcbLib':
                            self.assert_footprint(path.read_bytes(), names[row.part], originals[row.part])
                if combined:
                    payload = merged['PCBLIB'].read_bytes()
                    expected = list(names.values())
                    if previous:
                        expected.insert(0, prefix + '_ORIGINAL')
                        with olefile.OleFileIO(io.BytesIO(previous)) as before, olefile.OleFileIO(io.BytesIO(payload)) as after:
                            section = named_pcb_section(previous, expected[0])
                            for path in before.listdir():
                                if path[0] == section:
                                    self.assertEqual(before.openstream(path).read(), after.openstream(path).read())
                    self.assertEqual(self.names(payload), expected)

    def test_duplicate_names_use_suffixes_within_the_255_byte_index_limit(self):
        for length in (31, 32, 254, 255):
            with self.subTest(length=length):
                name = 'P' * length
                library = MergedLibrary('PCBLIB')
                for part in self.parts:
                    self.data[part]['packageDetail']['title'] = name
                    library.add(self.data[part], part, lambda: None)
                payload = library.build(self.parts, lambda: None)
                expected = [name, name[:253] + '_2']
                self.assertEqual(self.names(payload), expected)
                for part, actual in zip(self.parts, expected):
                    self.assert_footprint(payload, actual, export_pcblib(self.data[part], part))

    def test_short_names_with_the_same_sanitized_directory_stay_distinct(self):
        names = ['CONNECTOR*ANGLE', 'CONNECTOR?ANGLE']
        library = MergedLibrary('PCBLIB')
        for part, name in zip(self.parts, names):
            self.data[part]['packageDetail']['title'] = name
            library.add(self.data[part], part, lambda: None)
        payload = library.build(self.parts, lambda: None)
        self.assertEqual(self.names(payload), names)
        for part, name in zip(self.parts, names):
            self.assert_footprint(payload, name, export_pcblib(self.data[part], part))

    def test_equivalent_long_footprints_are_reused_and_aliases_are_diagnosable(self):
        folder = self.root / 'profile' / 'LCSC3D' / 'logs'
        logs.configure_logging(version='test', directory=folder)
        library = MergedLibrary('PCBLIB')
        name = 'LONG_SHARED_FOOTPRINT_' + 'A' * 50
        for part in self.parts:
            data = copy.deepcopy(self.data['C20197'])
            data['packageDetail']['title'] = name + ('_ALTERNATE' if part == 'C2040' else '')
            library.add(data, part, lambda: None)
        payload = library.build(self.parts, lambda: None)
        self.assertEqual(self.names(payload), [name])
        self.assertEqual(library.footprint_names, dict.fromkeys(self.parts, name))
        events = [json.loads(line) for path in folder.glob('LCSC3D*.log')
                  for line in path.read_text(encoding='utf-8').splitlines()]
        event = next(row for row in events if row['event'] == 'library.footprint_reuse')
        self.assertEqual((event['unique'], event['reused'], event['section_aliases']), (1, 1, 1))


if __name__ == '__main__':
    unittest.main()
