#!/usr/bin/env python3
import sys
import io
import os
import argparse
import codecs
from zipfile import ZipFile, ZipInfo
from modulefinder import ModuleFinder
from pathlib import Path
from importlib.metadata import distributions
from datetime import datetime

# Bootstrap code that will be saved as __main__.py in the zip
BOOTSTRAP_CODE = b"""exec(compile(
    # The zip from which __main__ was loaded:
    open(__loader__.archive, 'rb').read(
        # minimum offset = size of file before zip start:
        min(f[4] for f in getattr(__loader__, '_get_files', lambda: __loader__._files)().values())
    ).decode('utf8', 'surrogateescape'),
    __loader__.archive,     # set filename in code object
    'exec'                  # compile in 'exec' mode
))"""

MAIN_TIME = datetime(2000, 1, 1).timestamp()

def find_module_dependencies(script_path):
    # Convert script path to absolute and sys.path to Path objects
    script_path = Path(script_path).resolve()
    sys_paths = [script_path.parent if p == '' else Path(p) for p in sys.path]

    def find_base(module):
        if not module.__file__:
            return None
        modpath = Path(module.__file__)
        for base in sys_paths:
            try:
                return base, modpath.relative_to(base)
            except ValueError:
                continue
        return None

    # Find stdlib path using codecs module
    stdlib_base, _ = find_base(codecs)

    # Run modulefinder on target script
    finder = ModuleFinder()
    finder.run_script(str(script_path))

    # Yield non-stdlib dependencies
    for module in finder.modules.values():
        result = find_base(module)
        if not result:
            continue
        if result[0] == stdlib_base:
            continue
        yield result

def resolve_to_distributions(module_deps):
    # Build map of absolute file paths to distributions
    path_map = {}
    for dist in distributions():
        if not dist.files:
            continue
        base = Path(dist.locate_file(''))
        for f in dist.files:
            path_map[str(base / f)] = dist

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

def create_zip_archive(file_deps):
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w') as zf:
        # Add bootstrap code as __main__.py with fixed timestamp
        main = ZipInfo('__main__.py')
        main.date_time = datetime(2000, 1, 1).timetuple()[:6]
        zf.writestr(main, BOOTSTRAP_CODE)
        
        # Convert to list and sort by relative path
        entries = sorted(file_deps, key=lambda x: str(x[1]))
        for base, relpath in entries:
            fullpath = base / relpath
            if fullpath.exists():
                info = ZipInfo.from_file(fullpath, str(relpath))
                with open(fullpath, 'rb') as f:
                    zf.writestr(info, f.read())
    return buffer

def collect_from_directory(dirpath):
    """Generate base, relpath pairs from an unpacked directory"""
    base = Path(dirpath)
    for path in sorted(base.rglob('*')):
        if path.is_file():
            yield base, path.relative_to(base)

def find_script_size(path):
    """Find size of script without appended zip"""
    with open(path, 'rb') as f:
        data = f.read()
    with ZipFile(io.BytesIO(data)) as zf:
        return zf.start_dir

def pack(script_path, output_path=None):
    """Generate and append zip to script"""
    script_path = Path(script_path)
    output_path = output_path or script_path
    
    # Get original script timestamp and content
    orig_time = script_path.stat().st_mtime
    with open(script_path, 'rb') as f:
        script_content = f.read()

    # Create zip of dependencies
    deps = find_module_dependencies(script_path)
    mixed_deps = resolve_to_distributions(deps)
    file_deps = expand_distributions(mixed_deps)
    zip_buffer = create_zip_archive(file_deps)
    
    # Write combined file
    with open(output_path, 'wb') as f:
        f.write(script_content)
        f.write(zip_buffer.getvalue())
    
    # Restore timestamp
    os.utime(output_path, (orig_time, orig_time))

def strip(script_path, output_path=None):
    """Remove zip from file"""
    script_path = Path(script_path)
    output_path = output_path or script_path
    
    # Get original timestamp
    orig_time = script_path.stat().st_mtime
    
    if output_path != script_path:
        # Copy file first if output path different
        shutil.copy2(script_path, output_path)
    
    # Truncate to remove zip portion
    size = find_script_size(output_path)
    with open(output_path, 'r+b') as f:
        f.truncate(size)
        
    # Restore timestamp
    os.utime(output_path, (orig_time, orig_time))

def unpack(script_path, output_dir=None):
    """Extract zip to directory"""
    script_path = Path(script_path)
    if output_dir is None:
        output_dir = script_path.with_suffix('.d')
    else:
        output_dir = Path(output_dir)
    
    size = find_script_size(script_path)
    with open(script_path, 'rb') as f:
        data = f.read()
        zip_data = io.BytesIO(data[size:])
        with ZipFile(zip_data) as zf:
            zf.extractall(output_dir)
        
def repack(script_path, dir_path=None, output_path=None):
    """Generate zip from unpacked directory"""
    script_path = Path(script_path)
    output_path = output_path or script_path
    if dir_path is None:
        dir_path = script_path.with_suffix('.d')
    
    # Get original script timestamp and content
    orig_time = script_path.stat().st_mtime
    size = find_script_size(script_path)
    with open(script_path, 'rb') as f:
        orig_script = f.read(size)
    
    # Create zip from directory contents
    file_deps = collect_from_directory(dir_path)
    zip_buffer = create_zip_archive(file_deps)
    
    # Write combined file
    with open(output_path, 'wb') as f:
        f.write(orig_script)
        f.write(zip_buffer.getvalue())
    
    # Restore timestamp
    os.utime(output_path, (orig_time, orig_time))

def main():
    parser = argparse.ArgumentParser(description="CarryOn - Pack Python dependencies with scripts")
    parser.add_argument('command', choices=['pack', 'strip', 'unpack', 'repack'])
    parser.add_argument('script', help='Python script to process')
    parser.add_argument('-o', '--output', help='Output file/directory')
    parser.add_argument('-d', '--dir', help='Directory for repack')
    
    args = parser.parse_args()
    
    if args.command == 'pack':
        pack(args.script, args.output)
    elif args.command == 'strip':
        strip(args.script, args.output)
    elif args.command == 'unpack':
        unpack(args.script, args.output)
    elif args.command == 'repack':
        repack(args.script, args.dir, args.output)

if __name__ == '__main__':
    main()
