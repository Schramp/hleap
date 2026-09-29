"""Turn files from an extracted Android tree into Hansken test framework input.

Every file matching one of the given ALEAPP-style globs becomes a pair in the
output dir: <flat name>.raw (the file bytes) and <flat name>.trace (JSON with
file.name, file.path and file.extension), mimicking a trace Hansken extracted.

Usage:
    python tools/tree_to_testdata.py testdata/practical_exercise testdata/input \
        '*/com.google.android.apps.maps/files/new_recent_history_cache_search.cs'
"""
import fnmatch
import json
import os
import re
import shutil
import sys


def main(tree, out_dir, globs):
    os.makedirs(out_dir, exist_ok=True)
    for dirpath, _, filenames in os.walk(tree):
        for filename in filenames:
            source = os.path.join(dirpath, filename)
            # ALEAPP matches its globs against 'root/' + path relative to the input dir
            rel_path = os.path.relpath(source, tree).replace(os.sep, '/')
            if not any(fnmatch.fnmatch('root/' + rel_path, pattern) for pattern in globs):
                continue
            flat_name = re.sub(r'[^A-Za-z0-9]+', '_', rel_path).strip('_')
            file_meta = {'name': filename, 'path': '/' + rel_path}
            extension = os.path.splitext(filename)[1].lstrip('.')
            if extension:
                file_meta['extension'] = extension
            shutil.copyfile(source, os.path.join(out_dir, flat_name + '.raw'))
            with open(os.path.join(out_dir, flat_name + '.trace'), 'w') as trace_file:
                json.dump({'trace': {'file': file_meta}}, trace_file, indent=2)
                trace_file.write('\n')
            print(f'{rel_path} -> {flat_name}')


if __name__ == '__main__':
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
