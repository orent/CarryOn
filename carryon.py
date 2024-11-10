#!/usr/bin/env python3
import sys
import io
import os
import argparse
import sysconfig
import shutil
import zipfile
from modulefinder import ModuleFinder
from pathlib import Path
from datetime import datetime
try:
    from importlib.metadata import distributions
except ImportError:
    pass

# Bootstrap code that will be saved as __main__.py in the zip
BOOTSTRAP_CODE = b'''# Import zipext for extension module support
try:
    __import__('zipext')
except ImportError:
    pass
exec(compile(
    # The zip from which __main__ was loaded:
    open(__loader__.archive, 'rb').read(
        # minimum offset = size of file before zip start:
        min(f[4] for f in getattr(__loader__, '_get_files', lambda: __loader__._files)().values())
    ).decode('utf8', 'surrogateescape'),
    __loader__.archive,     # set filename in code object
    'exec'                  # compile in 'exec' mode
))'''

CARRYON_MARKER = b'\n\n##CarryOn bundled dependencies below this line##\n'


class ZipextNotFoundError(Exception):
    """Raised when zipext is needed but not available"""
    pass


def find_module_dependencies(script_path):
    # Convert script path to absolute and sys.path to Path objects
    script_path = Path(script_path).resolve()
    sys_paths = [script_path.parent if p == '' else Path(p) for p in sys.path]

    # Get stdlib paths
    stdlib_paths = {
        Path(sysconfig.get_path('stdlib')),
        Path(sysconfig.get_path('platstdlib')) / 'lib-dynload'
    }

    def find_base(module):
        if not module.__file__:
            return None
        modpath = Path(module.__file__).resolve()
        if modpath == script_path:  # Skip the script itself
            return None

        for base in sys_paths:
            try:
                relpath = modpath.relative_to(base)
                path_depth = len(relpath.parts)
                if relpath.name.startswith('__init__.'):
                    path_depth -= 1
                if path_depth == len(module.__name__.split('.')):
                    return base, relpath
            except ValueError:
                continue
        return None

    # Run modulefinder on target script
    finder = ModuleFinder()
    finder.run_script(str(script_path))

    # Yield non-stdlib dependencies
    for module in finder.modules.values():
        result = find_base(module)
        if not result:
            continue
        if result[0] in stdlib_paths:
            continue
        yield result


def normalize_excludes(excludes):
    """Convert exclude options to set of module names to exclude"""
    result = set()
    if not excludes:
        return result

    # Handle comma-separated lists and multiple occurrences
    for item in excludes:
        result.update(name.strip() for name in item.split(','))
    return result


def filter_mixed_deps(mixed_deps, excludes):
    """Filter mixed dependencies list based on exclude patterns"""
    if not excludes:
        yield from mixed_deps
        return

    excludes = normalize_excludes(excludes)
    for base, item in mixed_deps:
        # For distributions, check package name
        if hasattr(item, 'files'):
            if item.metadata['Name'] not in excludes:
                yield base, item
            continue

        # For module paths, check if path starts with any exclude pattern
        relpath = str(item)
        module_name = relpath.replace('/', '.').replace('\\', '.')
        if module_name.endswith('.py'):
            module_name = module_name[:-3]
        elif module_name.endswith('.pyc'):
            module_name = module_name[:-4]

        # Skip if module matches any exclude pattern
        if any(module_name == ex or module_name.startswith(ex + '.')
               for ex in excludes):
            continue

        yield base, item


def filter_file_deps(file_deps, excludes):
    """Filter file dependencies list based on exclude patterns"""
    if not excludes:
        yield from file_deps
        return

    excludes = normalize_excludes(excludes)
    for base, relpath in file_deps:
        # Convert path to module name format
        path_str = str(relpath)
        module_name = path_str.replace('/', '.').replace('\\', '.')
        if module_name.endswith('.py'):
            module_name = module_name[:-3]
        elif module_name.endswith('.pyc'):
            module_name = module_name[:-4]

        # Skip if module matches any exclude pattern
        if any(module_name == ex or module_name.startswith(ex + '.')
               for ex in excludes):
            continue

        yield base, relpath


def process_extension_modules(deps):
    """Process list looking for extension modules, adding zipext if needed"""
    has_extensions = False
    for base, relpath in deps:
        if str(relpath).endswith(('.so', '.pyd')):
            has_extensions = True
            break

    if has_extensions:
        # Try to find zipext module
        finder = ModuleFinder()
        try:
            finder.find_module('zipext')
        except ImportError:
            raise ZipextNotFoundError(
                "Extension modules found but zipext not available.\n"
                "Please install zipext with: pip install zipext"
            )

        # Find zipext location and add to deps
        for module in finder.modules.values():
            if module.__name__ == 'zipext':
                modpath = Path(module.__file__).resolve()
                for base in sys.path:
                    try:
                        relpath = modpath.relative_to(Path(base))
                        yield base, relpath
                        break
                    except ValueError:
                        continue

    # Pass through original deps
    yield from deps


def resolve_to_distributions(module_deps):
    # Build map of absolute file paths to distributions
    path_map = {}
    for dist in distributions():
        base = Path(dist.locate_file(''))
        for f in dist.files:
            path_map[str(base / f)] = dist

    # For each dependency, return distribution if found
    seen = set()
    for base, relpath in module_deps:
        fullpath = str(base / relpath)
        if fullpath not in path_map:
            yield base, relpath
            continue
        dist = path_map[fullpath]
        if dist in seen:
            continue
        seen.add(dist)
        yield base, dist


def expand_distributions(mixed_deps, exclude_pyc=True):
    for base, item in mixed_deps:
        # Pass through non-distribution items
        if not hasattr(item, 'files'):
            yield base, item
            continue

        # Expand distribution files
        base = Path(item.locate_file(''))
        for file in item.files:
            # Skip .pyc files if requested
            if exclude_pyc and file.name.endswith('.pyc'):
                continue
            # Skip files that try to escape package directory
            try:
                base.joinpath(file).relative_to(base)
            except ValueError:
                continue
            yield base, Path(file)


def create_zip_archive(file_deps, timestamp, uncompressed=False):
    buffer = io.BytesIO()
    compression = zipfile.ZIP_STORED if uncompressed else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, 'w', compression=compression) as zf:
        # Convert time to zip format
        date_time = datetime.fromtimestamp(timestamp).timetuple()[:6]

        # Add bootstrap code as __main__.py
        main = zipfile.ZipInfo('__main__.py')
        main.date_time = date_time
        main.compress_type = compression
        zf.writestr(main, BOOTSTRAP_CODE)

        entries = [(relpath, base) for base, relpath in file_deps]
        for relpath, base in sorted(entries):
            fullpath = base / relpath
            info = zipfile.ZipInfo.from_file(fullpath, str(relpath))
            info.date_time = date_time
            info.compress_type = compression
            zf.writestr(info, fullpath.read_bytes())
    return CARRYON_MARKER + buffer.getvalue()


def collect_from_directory(dirpath):
    """Generate base, relpath pairs from an unpacked directory"""
    base = Path(dirpath)
    for path in sorted(base.rglob('*')):
        if path.is_file():
            yield base, path.relative_to(base)


def find_script_size(path):
    """Find size of script without appended zip"""
    data = path.read_bytes()
    # First try to find the marker
    marker_pos = data.find(CARRYON_MARKER)
    if marker_pos != -1:
        return marker_pos

    # If no marker found, try to find zip header
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return zf.filelist[0].header_offset
    except zipfile.BadZipFile:
        return len(data)


def pack(script_path, output_path=None, uncompressed=False, skip_pkgs=False, excludes=None):
    """Generate and append zip to script"""
    script_path = Path(script_path)
    output_path = output_path or script_path

    # Create zip of dependencies with script's timestamp
    timestamp = script_path.stat().st_mtime
    deps = find_module_dependencies(script_path)
    try:
        deps = process_extension_modules(deps)
        if skip_pkgs:
            file_deps = filter_file_deps(deps, excludes)
        else:
            mixed_deps = resolve_to_distributions(deps)
            mixed_deps = filter_mixed_deps(mixed_deps, excludes)
            file_deps = expand_distributions(mixed_deps)
            file_deps = filter_file_deps(file_deps, excludes)
        zip = create_zip_archive(file_deps, timestamp, uncompressed)
    except ZipextNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # Create temporary file
    temp_path = output_path.with_suffix('.CarryOn.tmp')
    temp_path.write_bytes(script_path.read_bytes() + zip)

    # Copy metadata and replace target
    shutil.copystat(script_path, temp_path)
    temp_path.replace(output_path)


def strip(script_path, output_path=None):
    """Remove zip from file"""
    script_path = Path(script_path)
    output_path = output_path or script_path

    # Get size of original script before zip
    size = find_script_size(script_path)

    # Create temporary file
    temp_path = output_path.with_suffix('.CarryOn.tmp')
    data = script_path.read_bytes()[:size]
    temp_path.write_bytes(data)

    # Copy metadata and replace target
    shutil.copystat(script_path, temp_path)
    temp_path.replace(output_path)


def unpack(script_path, output_path=None):
    """Extract zip to directory and strip the script"""
    script_path = Path(script_path)
    if output_path is None:
        output_path = script_path
    else:
        output_path = Path(output_path)

    # Create the unpack directory
    unpack_dir = script_path.with_suffix('.d')
    if unpack_dir.exists():
        print(f"Note: {unpack_dir} already exists. Consider removing it first.")

    # Get size of original script before zip
    size = find_script_size(script_path)

    # Read and process data
    data = script_path.read_bytes()

    # Extract the zip contents
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [f for f in zf.filelist if f.filename != '__main__.py']
        zf.extractall(unpack_dir, members)

    # Create temporary file with stripped script
    temp_path = output_path.with_suffix('.CarryOn.tmp')
    temp_path.write_bytes(data[:size])

    # Copy metadata and replace target
    shutil.copystat(script_path, temp_path)
    temp_path.replace(output_path)


def repack(script_path, output_path=None, uncompressed=False, excludes=None):
    """Repack zip from unpacked directory and create stripped file"""
    script_path = Path(script_path)
    if output_path is None:
        output_path = script_path
    else:
        output_path = Path(output_path)
    dir_path = script_path.with_suffix('.d')

    # Get original script content without zip
    size = find_script_size(script_path)
    script_content = script_path.read_bytes()[:size]

    # Create zip from directory contents with script's timestamp
    timestamp = script_path.stat().st_mtime
    file_deps = collect_from_directory(dir_path)
    try:
        file_deps = process_extension_modules(file_deps)
        file_deps = filter_file_deps(file_deps, excludes)
        zip = create_zip_archive(file_deps, timestamp, uncompressed)
    except ZipextNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    # Create temporary file
    temp_path = output_path.with_suffix('.CarryOn.tmp')
    temp_path.write_bytes(script_content + zip)

    # Copy metadata and replace target
    shutil.copystat(script_path, temp_path)
    temp_path.replace(output_path)


# Where importlib.metadata is not available:
def distributions_fallback():
    class Dist:
        def __init__(self, dist):
            self.name = dist.name
            self._base = Path(dist.path).parent
            self.files = [Path(f[0]) for f in dist.list_installed_files()]

        def locate_file(self, path):
            return self._base / path
    from pip._vendor.distlib.database import DistributionPath
    return (Dist(d) for d in DistributionPath().get_distributions())


try:
    distributions
except NameError:
    distributions = distributions_fallback


def main():
    parser = argparse.ArgumentParser(
        description="CarryOn - Pack Python dependencies with scripts")
    parser.add_argument('command', choices=[
                        'pack', 'strip', 'unpack', 'repack'])
    parser.add_argument('script', type=Path, help='Python script to process')
    parser.add_argument('-o', '--output', type=Path, help='Output file')
    parser.add_argument('-0', '--uncompressed', action='store_true',
                        help='Store files uncompressed (default is to use deflate compression)')
    parser.add_argument('-m', '--modules-only', action='store_true',
                        help='Skip package resolution and pack module files directly')
    parser.add_argument('-x', '--exclude', action='append',
                        help='Exclude package or module (may be specified multiple times, comma-separated)')

    args = parser.parse_args()

    if args.command == 'pack':
        pack(args.script, args.output, args.uncompressed,
             args.modules_only, args.exclude)
    elif args.command == 'strip':
        strip(args.script, args.output)
    elif args.command == 'unpack':
        unpack(args.script, args.output)
    elif args.command == 'repack':
        repack(args.script, args.output, args.uncompressed, args.exclude)


if __name__ == '__main__':
    main()
