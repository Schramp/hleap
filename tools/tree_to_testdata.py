"""Turn files from an extracted Android tree into Hansken test framework input.

Every file matching the anchor glob becomes a pair in the output dir:
<flat name>.raw (the file bytes) and <flat name>.trace (JSON with file.name,
file.path, file.extension and file.modifiedOn), mimicking a trace Hansken
extracted. Files in the same directory whose name matches one of the
--siblings patterns (except the anchor) are written to <flat name>/searchtraces/, where the
standalone test framework serves them to searcher.search().

The source is a directory or a zip. Use the zip when timestamps matter: a git
checkout of the tree loses the original modification times. Zip times carry no
timezone, they are written as UTC.

Usage:
    python tools/tree_to_testdata.py testdata/practical_exercise.zip testdata/input \
        '*/com.google.android.apps.maps/databases/gmm_storage.db' --siblings 'gmm_storage.db*'
"""
import argparse
import datetime
import fnmatch
import json
import os
import posixpath
import re
import zipfile


def list_files(source):
    """Yield (relative posix path, modification time as UTC datetime, reader function) for every file in source."""
    if zipfile.is_zipfile(source):
        archive = zipfile.ZipFile(source)
        for info in archive.infolist():
            if not info.is_dir():
                modified = datetime.datetime(*info.date_time, tzinfo=datetime.timezone.utc)
                yield info.filename, modified, lambda info=info: archive.read(info)
        return
    for dirpath, _, filenames in os.walk(source):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            modified = datetime.datetime.fromtimestamp(os.stat(path).st_mtime, datetime.timezone.utc)
            yield os.path.relpath(path, source).replace(os.sep, '/'), modified, lambda path=path: _read(path)


def _read(path):
    with open(path, 'rb') as file:
        return file.read()


def flat_name(rel_path):
    return re.sub(r'[^A-Za-z0-9]+', '_', rel_path).strip('_')


def write_pair(out_dir, rel_path, modified, data, trace_id=None):
    name = posixpath.basename(rel_path)
    # file.path as Hansken writes it: relative, without a leading slash (the trace path does start with '/')
    file_meta = {'name': name, 'path': rel_path, 'modifiedOn': modified.strftime('%Y-%m-%dT%H:%M:%S.000Z')}
    extension = posixpath.splitext(name)[1].lstrip('.')
    if extension:
        file_meta['extension'] = extension
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, flat_name(rel_path) + '.raw'), 'wb') as raw_file:
        raw_file.write(data)
    with open(os.path.join(out_dir, flat_name(rel_path) + '.trace'), 'w') as trace_file:
        trace = {'file': file_meta}
        if trace_id:
            # search traces need an id (it becomes their uid) and data.raw.size, or the test framework search fails
            trace.update({'id': trace_id, 'name': name, 'path': '/' + rel_path, 'data': {'raw': {'size': len(data)}}})
        json.dump({'trace': trace}, trace_file, indent=2)
        trace_file.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('source', help='extracted tree: directory or zip')
    parser.add_argument('out_dir', help='test framework input dir, e.g. testdata/input')
    parser.add_argument('anchor', help="ALEAPP-style glob for the matched file(s), matched against 'root/<path>'")
    parser.add_argument('--siblings', nargs='*', default=[],
                        help='file name patterns of files in the anchor directory to make searchable')
    args = parser.parse_args()

    files = list(list_files(args.source))
    for rel_path, modified, read in files:
        # ALEAPP matches its globs against 'root/' + path relative to the input dir
        if not fnmatch.fnmatch('root/' + rel_path, args.anchor):
            continue
        write_pair(args.out_dir, rel_path, modified, read())
        print(f'{rel_path} -> {flat_name(rel_path)}')
        search_dir = os.path.join(args.out_dir, flat_name(rel_path), 'searchtraces')
        search_count = 0
        for sibling_path, sibling_modified, sibling_read in files:
            # the anchor itself is the trace under test, the plugin never needs to find it
            if (sibling_path != rel_path and posixpath.dirname(sibling_path) == posixpath.dirname(rel_path)
                    and any(fnmatch.fnmatch(posixpath.basename(sibling_path), pattern) for pattern in args.siblings)):
                search_count += 1
                write_pair(search_dir, sibling_path, sibling_modified, sibling_read(), f'search-{search_count}')
                print(f'  searchable: {sibling_path}')


if __name__ == '__main__':
    main()
