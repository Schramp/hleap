"""Check the ALEAPP anchor rules (issue #13) against an extracted tree and print the plan.

Shows which modules are dropped and why, which modules the tree would start, and optionally the generated
plugin matcher. Exits 1 when conflicts remain after the drops (never expected, the plan drops every needer).

Usage:
    python tools/anchor_check.py testdata/practical_exercise.zip [--matcher] [--aleapp ALEAPP]
                                 [--profile hleapp.alprofile] [--write-profile hleapp.alprofile]

--profile checks only the modules an ALEAPP profile selects; --write-profile writes a profile with the kept
modules the tree starts (the modules relevant for that tree).
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anchors import CONFLICT_DROPS, AnchorPlan, load_modules, load_profile, tree_paths, write_profile  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('source', help='extracted tree: directory or zip')
    parser.add_argument('--aleapp', default='ALEAPP', help='ALEAPP checkout (default: the submodule)')
    parser.add_argument('--matcher', action='store_true', help='print the generated HQL-Lite matcher')
    parser.add_argument('--profile', help='only check the modules this ALEAPP profile selects')
    parser.add_argument('--write-profile', help='write an ALEAPP profile with the kept modules the tree starts')
    args = parser.parse_args()

    modules = load_modules(args.aleapp)
    select = load_profile(args.profile, modules) if args.profile else None
    plan = AnchorPlan(modules, drop=CONFLICT_DROPS, select=select)
    files, dirs = tree_paths(args.source)
    conflicts = plan.check(files, dirs)

    kinds = collections.Counter(anchor.kind for anchor in plan.anchors.values())
    print(f'{len(plan.anchors)} modules ({kinds["file"]} file anchors, {kinds["dir"]} directory anchors), '
          f'{len(plan.kept)} kept, {len(plan.dropped)} dropped')
    reasons = collections.Counter(reason.split(',')[0].split(' ')[0] for reason in plan.dropped.values())
    print(f'  dropped: {dict(reasons)}')

    print(f'\nConflicts in {args.source} (needing module dropped): {len(conflicts)}')
    for path, key, owners in conflicts:
        print(f'  {key}: needs {path[len("root/"):]}, anchor of {", ".join(owners)}')

    print(f'\nAnchor traces in {args.source}:')
    started = 0
    for path in sorted(files | dirs):
        keys = plan.triggered_by(path, is_dir=path in dirs)
        if keys:
            started += len(keys)
            print(f'  {path[len("root/"):]}{"/" if path in dirs else ""}: {", ".join(keys)}')
    print(f'  {started} module runs')

    if args.write_profile:
        started = {key for path in files | dirs for key in plan.triggered_by(path, is_dir=path in dirs)}
        write_profile(args.write_profile, started)
        print(f'\nWrote {len(started)} modules to {args.write_profile}')

    if args.matcher:
        print(f'\nMatcher ({len(plan.kept)} anchors):\n{plan.matcher()}')


if __name__ == '__main__':
    main()
