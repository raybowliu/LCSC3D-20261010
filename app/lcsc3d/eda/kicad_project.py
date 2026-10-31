"""Register native libraries in a KiCad project's local library tables."""
import json
import os
from pathlib import Path

from lcsc3d.core.app_logging import log_event, traced
from lcsc3d.core.errors import DownloadError
from lcsc3d.eda.kicad import q
from lcsc3d.eda.kicad_append import publish_files, read_bytes, read_target
from lcsc3d.eda.kicad_sexpr import Document
from lcsc3d.services.output_transaction import checked_path


def read_project(path):
    path = checked_path(Path(path).expanduser())
    if path.suffix.lower() != '.kicad_pro':
        raise DownloadError('请选择有效的 .kicad_pro 工程文件')
    original = read_bytes(path, 4 * 1024 * 1024)
    try:
        value = json.loads(original.decode('utf-8-sig'))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DownloadError('KiCad 工程格式无法识别，未修改原文件') from exc
    if not isinstance(value, dict) or not isinstance(value.get('meta'), dict):
        raise DownloadError('KiCad 工程格式无法识别，未修改原文件')
    return original


def canonical_uri(uri, root):
    uri = uri.replace('${KIPRJMOD}', str(root))
    uri = os.path.expandvars(uri)
    if '${' in uri:
        return None
    return os.path.normcase(str((root / uri).resolve()))


@traced('library.kicad_project_import', lambda project, libraries, *a: {'count': len(libraries)}, level='INFO')
def add_libraries_to_project(project, libraries, check=lambda: None):
    project = checked_path(Path(project).expanduser())
    originals = {project: read_project(project)}
    selected = {}
    for path in dict.fromkeys(Path(path).resolve() for path in libraries):
        fmt = {'.kicad_sym': 'KICAD_SYMBOL', '.pretty': 'KICAD_FOOTPRINT'}.get(path.suffix.lower())
        if fmt is None:
            raise DownloadError('仅能导入已有的 .kicad_sym 符号库和 .pretty 封装库目录')
        check()
        target = read_target(path, fmt, check)
        originals.update(target.files)
        selected.setdefault(fmt, []).append(path)
    updates, added, skipped = {}, 0, 0
    for fmt, name, kind in (('KICAD_SYMBOL', 'sym-lib-table', 'sym_lib_table'),
                            ('KICAD_FOOTPRINT', 'fp-lib-table', 'fp_lib_table')):
        if fmt not in selected:
            continue
        table = checked_path(project.parent / name)
        original = read_bytes(table, 4 * 1024 * 1024) if table.exists() else None
        originals[table] = original
        document = Document(original if original is not None else f'({kind}\n  (version 7)\n)\n'.encode(), kind, check)
        known = {}
        for node in document.root.children('lib'):
            fields = {}
            for key in ('name', 'uri', 'type'):
                values = node.children(key)
                if len(values) != 1:
                    raise DownloadError('KiCad 工程库表字段不完整，未修改原文件')
                fields[key] = values[0].atom(1)
            if fields['name'] in known:
                raise DownloadError('KiCad 工程库表含重复昵称，请先在 KiCad 中整理库表')
            known[fields['name']] = fields
        additions = []
        for path in selected[fmt]:
            check()
            nickname = path.stem
            if nickname in known:
                row = known[nickname]
                if row['type'] != 'KiCad' or canonical_uri(row['uri'], project.parent) != os.path.normcase(str(path)):
                    raise DownloadError('工程中同名库昵称已指向其他库，请更换库名或在 KiCad 中调整引用：' + nickname)
                skipped += 1
                continue
            try:
                uri = '${KIPRJMOD}/' + Path(os.path.relpath(path, project.parent)).as_posix()
            except ValueError:
                uri = path.as_posix()
            additions.append(f'  (lib (name {q(nickname)})(type "KiCad")(uri {q(uri)})(options "")(descr ""))')
            added += 1
        if additions:
            updates[table] = document.insert(additions)
            if len(updates[table]) > 4 * 1024 * 1024:
                raise DownloadError('追加后的 KiCad 工程库表超过 4 MiB，未修改原文件')
    publish_files(updates, originals, check)
    log_event('INFO', 'library.kicad_project_registered', added=added, skipped=skipped, tables=len(updates))
    return {'added': added, 'skipped': skipped, 'backup': ''}
