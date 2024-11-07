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
from importlib.metadata import distributions
from datetime import datetime

# Bootstrap code that will be saved as __main__.py in the zip
BOOTSTRAP_CODE = b'''exec(compile(
    # The zip from which __main__ was loaded:
    open(__loader__.archive, 'rb').read(
        # minimum offset = size of file before zip start:
        min(f[4] for f in getattr(__loader__, '_get_files', lambda: __loader__._files)().values())
    ).decode('utf8', 'surrogateescape'),
    __loader__.archive,     # set filename in code object
    'exec'                  # compile in 'exec' mode
))'''

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
    return buffer.getvalue()

def collect_from_directory(dirpath):
    """Generate base, relpath pairs from an unpacked directory"""
    base = Path(dirpath)
    for path in sorted(base.rglob('*')):
        if path.is_file():
            yield base, path.relative_to(base)

def find_script_size(path):
    """Find size of script without appended zip"""
    data = path.read_bytes()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return zf.filelist[0].header_offset
    except zipfile.BadZipFile:
        return len(data)

def pack(script_path, output_path=None, uncompressed=False):
    """Generate and append zip to script"""
    script_path = Path(script_path)
    output_path = output_path or script_path

    # Create zip of dependencies with script's timestamp
    timestamp = script_path.stat().st_mtime
    deps = find_module_dependencies(script_path)
    mixed_deps = resolve_to_distributions(deps)
    file_deps = expand_distributions(mixed_deps)
    zip = create_zip_archive(file_deps, timestamp, uncompressed)
    
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

def unpack(script_path, output_dir=None):
    """Extract zip to directory"""
    script_path = Path(script_path)
    if output_dir is None:
        output_dir = script_path.with_suffix('.d')
    else:
        output_dir = Path(output_dir)
    
    if output_dir.exists():
        print(f"Note: directory {output_dir} already exists. Consider removing it first.")
    
    data = script_path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        members = [f for f in zf.filelist if f.filename != '__main__.py']
        zf.extractall(output_dir, members)

def repack(script_path, output_path=None, uncompressed=False):
    """Generate zip from unpacked directory"""
    script_path = Path(script_path)
    output_path = output_path or script_path
    dir_path = script_path.with_suffix('.d')
    
    # Get original script timestamp
    size = find_script_size(script_path)
    data = script_path.read_bytes()
    timestamp = script_path.stat().st_mtime

    # Create zip from directory contents with script's timestamp
    file_deps = collect_from_directory(dir_path)
    zip = create_zip_archive(file_deps, timestamp, uncompressed)

    # Create temporary file
    temp_path = output_path.with_suffix('.CarryOn.tmp')
    temp_path.write_bytes(data[:size] + zip)
    
    # Copy metadata and replace target
    shutil.copystat(script_path, temp_path)
    temp_path.replace(output_path)

def main():
    parser = argparse.ArgumentParser(description="CarryOn - Pack Python dependencies with scripts")
    parser.add_argument('command', choices=['pack', 'strip', 'unpack', 'repack'])
    parser.add_argument('script', type=Path, help='Python script to process')
    parser.add_argument('-o', '--output', type=Path, help='Output file/directory')
    parser.add_argument('-0', '--uncompressed', action='store_true', 
                      help='Store files uncompressed (default is to use deflate compression)')
    
    args = parser.parse_args()
    
    if args.command == 'pack':
        pack(args.script, args.output, args.uncompressed)
    elif args.command == 'strip':
        strip(args.script, args.output)
    elif args.command == 'unpack':
        unpack(args.script, args.output)
    elif args.command == 'repack':
        repack(args.script, args.output, args.uncompressed)

if __name__ == '__main__':
    main()