"""Preserve existing KiCad libraries, append new members and resolve their links."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import tempfile

from lcsc3d.core.app_logging import log_event, record_error, traced
from lcsc3d.core.app_paths import temporary_directory
from lcsc3d.core.errors import DownloadError
from lcsc3d.eda.kicad import (FORMATS, build_library, footprint_definition, identifier,
                             library_name, q, symbol_definition, symbol_library)
from lcsc3d.eda.kicad_sexpr import Atom, Document, Node
from lcsc3d.services.output_transaction import checked_path, output_change

MAX_LIBRARY_BYTES = 256 * 1024 * 1024


def read_bytes(path, limit=MAX_LIBRARY_BYTES):
    path = checked_path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise DownloadError('请选择有效的 KiCad 库文件（不超过 256 MiB）')
    with path.open('rb') as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise DownloadError('KiCad 库文件过大')
    return payload


@dataclass
class LibraryTarget:
    path: Path
    format: str
    exists: bool
    files: dict
    document: Document | None = None

    def check(self):
        checked_path(self.path)
        if self.path.exists() != self.exists:
            raise DownloadError('目标库在处理期间发生变化，请重新执行追加；保留当前文件')
        if self.format == 'KICAD_FOOTPRINT' and self.exists:
            current = {checked_path(p) for p in self.path.rglob('*') if p.is_file()}
            if current != set(self.files):
                raise DownloadError('封装库在处理期间发生变化，请重新执行追加；保留当前文件')
        for path, payload in self.files.items():
            if read_bytes(path) != payload:
                raise DownloadError('目标库在处理期间已被修改，请重新执行；保留当前文件')


@traced('library.kicad_read', lambda path, fmt, *a, **kw: {'format': fmt}, level='INFO')
def read_target(path, fmt, check=lambda: None, *, allow_new=False):
    path = checked_path(Path(path).expanduser())
    suffix = '.kicad_sym' if fmt == 'KICAD_SYMBOL' else '.pretty'
    if fmt not in FORMATS or path.suffix.lower() != suffix:
        raise DownloadError('请选择 .kicad_sym 符号库或 .pretty 封装库目录')
    library_name(path.stem)
    if not path.exists():
        if not allow_new:
            raise DownloadError('指定的 KiCad 库不存在')
        return LibraryTarget(path, fmt, False, {})
    if fmt == 'KICAD_SYMBOL':
        payload = read_bytes(path)
        document = Document(payload, 'kicad_symbol_lib', check)
        versions = document.root.children('version')
        if len(versions) != 1 or not versions[0].atom(1).isdigit() or int(versions[0].atom(1)) < 20211014:
            raise DownloadError('请选择 KiCad 6 或更新版本的 .kicad_sym 符号库')
        for node in document.root.children('symbol'):
            node.atom(1)
        return LibraryTarget(path, fmt, True, {path: payload}, document)
    if not path.is_dir():
        raise DownloadError('.pretty 封装库必须是目录')
    files, size = {}, 0
    for entry in path.rglob('*'):
        check()
        entry = checked_path(entry)
        if entry.is_file():
            payload = read_bytes(entry, MAX_LIBRARY_BYTES - size)
            size += len(payload)
            files[entry] = payload
            if entry.parent == path and entry.suffix.lower() == '.kicad_mod':
                node = Document(payload, check=check).root
                if node.atom() not in ('footprint', 'module'):
                    raise DownloadError('封装库含无效的 .kicad_mod 文件')
                node.atom(1)
    return LibraryTarget(path, fmt, True, files)


def footprint_key(payload, model_payloads=None):
    """Ignore names and editor metadata; keep all physical/unknown data significant."""
    root = Document(payload).root

    def key(value, top=False):
        if isinstance(value, Atom):
            if not value.quoted:
                try:
                    number = Decimal(value.value)
                    if number.is_finite():
                        return ('number', number)
                except InvalidOperation:
                    pass
            return ('string' if value.quoted else 'atom', value.value)
        kind = value.atom()
        if kind in ('uuid', 'tstamp', 'tedit'):
            return None
        if top and (kind in ('descr', 'tags', 'version', 'generator', 'generator_version') or
                    kind == 'fp_text' and value.atom(1) in ('reference', 'value') or
                    kind == 'property' and value.atom(1) in ('Reference', 'Value')):
            return None
        values = value.values[2:] if value is root else value.values
        if kind == 'model' and model_payloads and value.atom(1) in model_payloads:
            return (('model', hashlib.sha256(model_payloads[value.atom(1)]).hexdigest()),
                    *(key(item) for item in values[2:]))
        return tuple(result for item in values if (result := key(item, value is root)) is not None)
    return key(root)


@traced('library.kicad_publish', lambda updates, originals, *a: {'count': len(updates)}, level='INFO')
def publish_files(updates, originals, check=lambda: None):
    """Publish a small change set with backups, exclusive creation and rollback."""
    from lcsc3d.eda.altium_project import commit_user_file
    from lcsc3d.eda.library_files import _move_new
    from lcsc3d.services.backend import atomic_write
    updates = {checked_path(path): payload for path, payload in updates.items()}
    originals = {checked_path(path): payload for path, payload in originals.items()}
    if any(len(payload) > MAX_LIBRARY_BYTES for payload in updates.values()):
        raise DownloadError('追加后的 KiCad 库文件超过 256 MiB，未修改原文件')

    def unchanged(path, payload):
        checked_path(path)
        if (read_bytes(path) if path.exists() else None) != payload:
            raise DownloadError('文件在处理期间已被修改，请重新执行；保留当前文件')

    for path, payload in originals.items():
        check()
        unchanged(path, payload)
    journal, made_dirs = [], set()
    with tempfile.TemporaryDirectory(prefix='kicad-append-', dir=temporary_directory()) as staging:
        try:
            for index, (path, payload) in enumerate(updates.items()):
                check()
                original = originals.get(path)
                unchanged(path, original)
                if payload == original:
                    continue
                parent = path.parent
                while not parent.exists():
                    made_dirs.add(parent)
                    parent = parent.parent
                journal.append((path, original, hashlib.sha256(payload).hexdigest()))
                if original is not None:
                    commit_user_file(path, original, payload, check)
                else:
                    source = Path(staging) / str(index)
                    source.write_bytes(payload)
                    with output_change(path, hashlib.sha256(payload).hexdigest()):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        _move_new(source, path)
            check()
            for path, original in originals.items():
                unchanged(path, updates.get(path, original))
        except BaseException as exc:
            failures = []
            for path, original, written_hash in reversed(journal):
                try:
                    current = read_bytes(path) if checked_path(path).exists() else None
                    if current == original:
                        continue
                    if current is None or hashlib.sha256(current).hexdigest() != written_hash:
                        raise OSError('文件在恢复前发生变化，保留外部修改')
                    if original is None:
                        with output_change(path, None):
                            path.unlink()
                    else:
                        atomic_write(path, original)
                except Exception as rollback_error:
                    record_error(rollback_error, 'library.kicad_rollback_file_failed', stage='rollback')
                    failures.append(path)
            for directory in sorted(made_dirs, key=lambda p: len(p.parts), reverse=True):
                try:
                    checked_path(directory).rmdir()
                except OSError:
                    pass
            log_event('ERROR' if failures else 'INFO', 'library.kicad_rollback_failed' if failures else
                      'library.kicad_rollback_completed', count=len(journal), failure_count=len(failures))
            if failures:
                recovery = Path(tempfile.mkdtemp(prefix='kicad-append-recovery-', dir=temporary_directory()))
                manifest = []
                for index, (path, original, written_hash) in enumerate(journal):
                    backup = str(index) + '.bin' if original is not None else ''
                    if backup:
                        (recovery / backup).write_bytes(original)
                    manifest.append({'target': str(path), 'backup': backup, 'written_sha256': written_hash})
                (recovery / 'recovery.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
                log_event('ERROR', 'library.kicad_recovery_preserved', count=len(manifest), recovery_preserved=True)
                raise DownloadError('追加失败，部分文件无法恢复；原文件恢复资料保留在：' + str(recovery)) from exc
            raise


@traced('export.kicad_append', lambda results, entries, options, *a, **kw: {'count': len(entries)}, level='INFO')
def append_batch(results, entries, options, check, targets, target_errors, on_progress=None):
    from lcsc3d.eda.library_files import commit_library
    from lcsc3d.eda.kicad import conversion_warnings
    rows = [row for row in results if row.part in entries]
    paths = options.merged_paths()
    formats = {row.part: tuple(fmt for fmt in row.pending_formats if fmt in FORMATS) for row in rows}
    references, completed, errors, updates, originals = {}, {}, dict(target_errors), {}, {}
    skipped, reused = 0, 0
    for target in targets.values():
        originals.update(target.files)
        if target.format == 'KICAD_SYMBOL' and not target.exists:
            originals[target.path] = None
    footprint_target = targets.get('KICAD_FOOTPRINT')
    known, used = {}, set()
    if footprint_target:
        model_payloads = {path.relative_to(footprint_target.path).as_posix(): raw for path, raw in footprint_target.files.items()}
        for path, payload in footprint_target.files.items():
            if path.parent == footprint_target.path and path.suffix.lower() == '.kicad_mod':
                used.add(path.stem.casefold())
                known.setdefault(footprint_key(payload, model_payloads), path)
    definitions, symbol_names = [], set()
    symbol_target = targets.get('KICAD_SYMBOL')
    if symbol_target and symbol_target.document:
        symbol_names = {node.atom(1) for node in symbol_target.document.root.children('symbol')}
    batch_names = set()
    for row in rows:
        check()
        if on_progress:
            on_progress(row.part, .85)
        component, folder, assets = entries[row.part]
        if 'KICAD_FOOTPRINT' in formats[row.part] and footprint_target:
            try:
                if component.footprint is None:
                    raise DownloadError('器件缺少封装数据')
                candidate = identifier(component.footprint.name, component.part + '_Footprint')
                payload = footprint_definition(component, candidate, check, model_assets=assets if options.bind_3d_models else None,
                                               model_mode=options.kicad_3d_mode)
                pending_models = {}
                # External models are immutable resources. Content-qualified names
                # avoid overwriting models used by existing footprints.
                if options.bind_3d_models and options.kicad_3d_mode == 'external':
                    from lcsc3d.models.model_export import resolved_models
                    for model, asset, raw in resolved_models(component, assets):
                        name = model.asset_id + '_' + hashlib.sha256(raw).hexdigest()[:16] + '.step'
                        payload = payload.replace(q('LCSC3D.3dshapes/' + model.asset_id + '.step').encode(),
                                                  q('LCSC3D.3dshapes/' + name).encode())
                        model_path = footprint_target.path / 'LCSC3D.3dshapes' / name
                        if model_path in originals and originals[model_path] != raw:
                            raise DownloadError('已有 3D 模型内容与资源名称不一致')
                        pending_models[model_path] = raw
                signature = footprint_key(payload, {path.relative_to(footprint_target.path).as_posix(): raw
                    for path, raw in {**footprint_target.files, **updates, **pending_models}.items()
                    if path.is_relative_to(footprint_target.path)})
                path = known.get(signature)
                if path is None:
                    name, index = candidate, 2
                    while name.casefold() in used:
                        name, index = candidate + '_' + str(index), index + 1
                    if name != candidate:
                        document = Document(payload)
                        atom = document.root.values[1]
                        payload = document.encode(document.text[:atom.start] + q(name) + document.text[atom.end:])
                    path = footprint_target.path / (name + '.kicad_mod')
                    used.add(name.casefold())
                    known[signature] = path
                    updates.update(pending_models)
                    updates[path] = payload
                else:
                    reused += 1
                references[row.part] = footprint_target.path.stem + ':' + path.stem
                completed[(row.part, 'KICAD_FOOTPRINT')] = path
            except DownloadError as exc:
                record_error(exc, 'export.kicad_append_conversion_failed', part=row.part, format='KICAD_FOOTPRINT', stage='convert')
                errors[(row.part, 'KICAD_FOOTPRINT')] = str(exc)
        if 'KICAD_SYMBOL' in formats[row.part] and symbol_target:
            try:
                if component.symbol is None:
                    raise DownloadError('器件缺少符号数据')
                name = identifier(component.symbol.name, row.part)
                if name in batch_names:
                    name += '_' + row.part
                if name in batch_names:
                    raise DownloadError('KiCad 符号名称冲突')
                batch_names.add(name)
                if name in symbol_names:
                    skipped += 1
                else:
                    definitions.append(symbol_definition(component, name, references.get(row.part, ''), check,
                                                         electrical_policy=options.kicad_electrical_policy))
                    symbol_names.add(name)
                completed[(row.part, 'KICAD_SYMBOL')] = symbol_target.path
            except DownloadError as exc:
                record_error(exc, 'export.kicad_append_conversion_failed', part=row.part, format='KICAD_SYMBOL', stage='convert')
                errors[(row.part, 'KICAD_SYMBOL')] = str(exc)
    if symbol_target and definitions:
        updates[symbol_target.path] = (symbol_target.document.insert(definitions) if symbol_target.document else
                                       symbol_library(definitions))
    conversion_errors = {key for key in errors if isinstance(key, tuple)}
    try:
        check()
        for target in targets.values():
            target.check()
        if footprint_target and sum(len(raw) for path, raw in {**footprint_target.files, **updates}.items()
                                    if path.is_relative_to(footprint_target.path)) > MAX_LIBRARY_BYTES:
            raise DownloadError('追加后的 KiCad 封装库超过 256 MiB，未修改原文件')
        for row in rows:
            if on_progress:
                on_progress(row.part, .90)
        publish_files(updates, originals, check)
        log_event('INFO', 'download.library_committed', target='kicad', mode='append', count=len(rows),
                  skipped=skipped, reused=reused, files=len(updates))
    except Exception as exc:
        from lcsc3d.core.errors import Cancelled
        if isinstance(exc, Cancelled):
            raise
        record_error(exc, 'download.kicad_failed', stage='append')
        for key in completed:
            errors[key] = str(exc)
        completed.clear()
    for row in rows:
        component, folder, assets = entries[row.part]
        done = []
        for fmt in formats[row.part]:
            error = errors.get((row.part, fmt), errors.get(fmt))
            if error:
                message = ('KiCad 符号库' if fmt == 'KICAD_SYMBOL' else 'KiCad 封装库') + '：' + error
                row.export_errors.append(message)
                row.message = '；'.join(filter(None, (row.message, message)))
                log_event('ERROR', 'download.format_failed', part=row.part, format=fmt,
                          stage='read' if fmt in target_errors else 'convert' if (row.part, fmt) in conversion_errors else 'append')
            elif (row.part, fmt) in completed:
                row.files.append(str(completed[(row.part, fmt)]))
                row.completed_formats.append(fmt)
                log_event('INFO', 'download.format_completed', part=row.part, format=fmt, mode='append')
                done.append(fmt)
        if done:
            row.folder = str(next(iter(paths.values())).parent)
            row.message = '；'.join(filter(None, (row.message, f'已追加 KiCad 元件库；原库保留 {skipped} 个同名符号，已复用 {reused} 个相同 footprint')))
            warnings = conversion_warnings(component, done)
            if warnings:
                row.message += '；KiCad：' + '，'.join(warnings)
                log_event('WARNING', 'export.approximation', part=row.part, target='kicad', warnings=list(warnings))
        if options.keep_individual and done:
            try:
                names = options.kicad_names()
                artifacts, failures = build_library([component], {row.part: tuple(done)}, 'LCSC3D', check,
                    electrical_policy=options.kicad_electrical_policy, model_assets=assets if options.bind_3d_models else None,
                    model_mode=options.kicad_3d_mode,
                    symbol_libraries={row.part: names['KICAD_SYMBOL'] + '_' + row.part},
                    footprint_libraries={row.part: names['KICAD_FOOTPRINT'] + '_' + row.part},
                    footprint_references=references)
                for artifact in artifacts:
                    output = (Path(folder) / artifact.path).resolve()
                    library_root = output.parent if artifact.format == 'KICAD_FOOTPRINT' else None
                    for fmt, central in paths.items():
                        if (output == central or fmt == 'KICAD_FOOTPRINT' and output.is_relative_to(central)
                                or library_root and central.is_relative_to(library_root)):
                            raise DownloadError('独立导出与指定库路径相同，请关闭“独立导出器件”或另选保存目录')
                written = commit_library(folder, artifacts, check, overwrite=True)
                row.files.extend(path for artifact, path in written)
                row.folder = str(folder)
                for (part, fmt), error in failures.items():
                    raise DownloadError(fmt + '：' + error)
                row.message += '；已独立导出器件'
            except Exception as exc:
                from lcsc3d.core.errors import Cancelled
                if isinstance(exc, Cancelled):
                    raise
                record_error(exc, 'download.kicad_individual_failed', part=row.part, stage='write')
                row.export_errors.append('独立导出：' + str(exc))
                row.message += '；' + row.export_errors[-1]
        if on_progress:
            on_progress(row.part, .95)
