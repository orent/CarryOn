#!/usr/bin/env python3
"""CarryOn - Pack your Python script with its dependencies

# Installation

```bash
pip install carryon
```

Or better, since this is a command-line tool:
```bash
pipx install carryon
```

# Usage

```bash
carryon script.py
```

This creates a self-contained executable that includes all non-stdlib dependencies.

Options:
- `-o, --output` - Specify output file
- `-p, --packages` - Include complete packages
- `-f, --file FILE ARCNAME` - Add extra files to the bundle

# How it works

Carryon appends a ZIP archive with all dependencies to your script, making it
self-contained while still being a valid Python script.

Not a heavyweight solution like PyInstaller and other "checked luggage" tools.
It still requires a python interpreter.

The script portion can still be edited after packaging - just ensure your editor
preserves binary data and doesn't add a newline at EOF. For vim, use:
    vim -b script_packaged.py   # binary mode

# License

MIT License

Copyright (c) 2024 Oren Tirosh

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import modulefinder
import sys
import os
import zipfile
import codecs
from importlib.util import find_spec
from pathlib import Path
from io import BytesIO

# Get stdlib base path using a known stdlib module
STDLIB_PATH = Path(codecs.__file__).resolve().parent

# Bootstrap code that will be saved as __main__.py in the zip
BOOTSTRAP_CODE = b"""exec(compile(
    # The zip from which __main__ was loaded:
    open(__loader__.archive, 'rb').read(
        # minimum offset = size of file before zip start:
        min(f[4] for f in __loader__._files.values())
    ).decode('utf8', 'surrogateescape'),
    __loader__.archive,     # set filename in code object
    'exec'                  # compile in 'exec' mode
))"""

def get_dependencies(script_path):
    """Find all dependencies of a Python script."""
    finder = modulefinder.ModuleFinder()
    finder.run_script(str(script_path))
    return finder.modules.keys()

def is_stdlib_module(module_name):
    """Check if a module is part of the Python standard library."""
    try:
        spec = find_spec(module_name)
        if spec is None or not spec.has_location:  # Built-in or frozen modules
            return True

        stdlib_path = str(STDLIB_PATH)
        return str(Path(spec.origin).resolve()).startswith(stdlib_path)
    except (ImportError, AttributeError, ValueError):
        return False

def get_script_content(script_path):
    """Read script content and truncate any existing ZIP."""
    try:
        # Try to open it as a zip file first
        with zipfile.ZipFile(script_path, 'r') as zf:
            # Get the minimum offset - same method used in __main__.py
            size = min(f[4] for f in zf.filelist)
    except zipfile.BadZipFile:
        size = 999999999

    with open(script_path, 'rb') as f:
        return f.read(size)

def find_base_dir(spec, script_path):
    """Find which sys.path entry a module is under."""
    module_path = Path(spec.origin).resolve()
    
    for path in sys.path:
        if path == '':
            path = script_path.parent
        else:
            path = Path(path).resolve()
            
        try:
            module_path.relative_to(path)
            return path
        except ValueError:
            continue
    return None

def _build_package_map_metadata():
    """Build package maps using importlib.metadata."""
    from importlib.metadata import distributions
    file_to_pkg = {}      # Path -> pkg_name
    pkg_to_files = {}     # pkg_name -> set(Path)
    pkg_to_base = {}      # pkg_name -> base_path
    
    for dist in distributions():
        try:
            base = dist.locate_file('.').resolve()
            pkg_to_base[dist.name] = base
            for file in dist.files:
                if file.name.endswith('.pyc'):
                    continue
                try:
                    path = dist.locate_file(file).resolve()
                    file_to_pkg[path] = dist.name
                    pkg_to_files.setdefault(dist.name, set()).add(path)
                except Exception:
                    continue
        except Exception as e:
            print(f"Warning: Error processing package {dist.name}: {e}", 
                  file=sys.stderr)
            
    return file_to_pkg, pkg_to_files, pkg_to_base

def _build_package_map_resources():
    """Build package maps using pkg_resources."""
    import pip._vendor.pkg_resources as pkg_resources
    file_to_pkg = {}
    pkg_to_files = {}
    pkg_to_base = {}
    
    for dist in pkg_resources.working_set:
        base = Path(dist.location)
        pkg_to_base[dist.key] = base
        try:
            record = base / f"{dist.egg_info}/RECORD"
            if not record.exists():
                continue
            with open(record) as f:
                for line in f:
                    filename = line.split(',')[0]
                    if filename.endswith('.pyc'):
                        continue
                    try:
                        path = (base / filename).resolve()
                        if not base in path.parents:
                            continue
                        file_to_pkg[path] = dist.key
                        pkg_to_files.setdefault(dist.key, set()).add(path)
                    except (ValueError, OSError):
                        continue
        except Exception as e:
            print(f"Warning: Error processing package {dist.key}: {e}", 
                  file=sys.stderr)
        
    return file_to_pkg, pkg_to_files, pkg_to_base

try:
    from importlib.metadata import distributions
    build_package_map = _build_package_map_metadata
except ImportError:
    build_package_map = _build_package_map_resources

def add_file_to_zip(zipf, file_path, arcname, processed_files):
    """Add a file to the zip if not already processed."""
    if arcname in processed_files:
        return False
    processed_files.add(arcname)
    zipf.write(file_path, arcname)
    return True

def extend_file_list(module_paths, include_packages=False):
    """Get complete list of files to include based on modulefinder results."""
    if not include_packages:
        return module_paths
        
    file_to_pkg, pkg_to_files, _ = build_package_map()
    files_to_include = set()
    processed_packages = set()
    
    # For each module found by modulefinder
    for path in module_paths:
        if path in file_to_pkg:
            # Module belongs to a package
            pkg_name = file_to_pkg[path]
            if pkg_name not in processed_packages:
                processed_packages.add(pkg_name)
                # Add all files from this package
                files_to_include.update(pkg_to_files[pkg_name])
        else:
            # Non-packaged module
            files_to_include.add(path)
            
    return files_to_include

def create_archive(script_path, files_to_include, extra_files=None):
    """Create ZIP archive with script and specified files."""
    script_content = get_script_content(script_path)
    
    # Get package info for base path lookup
    try:
        file_to_pkg, _, pkg_to_base = build_package_map()
    except Exception:
        file_to_pkg = {}
        pkg_to_base = {}
        
    zip_buffer = BytesIO()
    processed_files = set()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Add __main__.py that executes the script portion
        zipf.writestr('__main__.py', BOOTSTRAP_CODE)

        # Add any extra files first
        if extra_files:
            for file_path, arc_path in extra_files:
                if file_path.exists():
                    add_file_to_zip(zipf, file_path, arc_path, processed_files)
                else:
                    print(f"Warning: Extra file not found: {file_path}", 
                          file=sys.stderr)

        # Add all files using appropriate base paths
        for path in files_to_include:
            try:
                # Try package base path first
                if path in file_to_pkg:
                    pkg_name = file_to_pkg[path]
                    base = pkg_to_base[pkg_name]
                else:
                    # Fall back to sys.path search
                    spec = find_spec(path.stem)
                    if not spec:
                        continue
                    base = find_base_dir(spec, script_path)
                    if not base:
                        continue
                
                arcname = path.relative_to(base)
                add_file_to_zip(zipf, path, arcname, processed_files)
            except Exception as e:
                print(f"Warning: Error adding file {path}: {e}", file=sys.stderr)
                
    return script_content + b'\n\n# === Bundled dependencies follow this line ===\n' + zip_buffer.getvalue()

def package_with_script(script_path, output_path=None, *, include_packages=False, 
                       extra_files=None):
    """Package script with its dependencies into a self-contained file."""
    if output_path is None:
        output_path = script_path.parent / (script_path.stem + '_carryon' + script_path.suffix)

    # Get list of module paths from modulefinder
    modules = get_dependencies(script_path)
    module_paths = set()
    for module_name in modules:
        if module_name == '__main__' or is_stdlib_module(module_name):
            continue
        try:
            spec = find_spec(module_name)
            if not spec or not spec.has_location:
                continue
            module_paths.add(Path(spec.origin).resolve())
        except (ModuleNotFoundError, ValueError) as e:
            print(f"Warning: Module not found {module_name}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Error finding module {module_name}: {e}", file=sys.stderr)
    
    # Get complete list of files to include
    files_to_include = extend_file_list(module_paths, include_packages)
    
    # Create the archive
    archive_content = create_archive(script_path, files_to_include, extra_files)
    
    # Write output file
    output_path.write_bytes(archive_content)
    output_path.chmod(0o755)
    return output_path

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Package Python script with its dependencies')
    parser.add_argument('script', type=Path, help='Python script to package')
    parser.add_argument('-o', '--output', type=Path, help='Output path')
    parser.add_argument('-p', '--packages', action='store_true',
                       help='Include complete packages, not just imported modules')
    parser.add_argument('-f', '--file', action='append', nargs=2,
                       metavar=('FILE', 'ARCNAME'), type=Path,
                       help='Add extra file to zip with given archive path')

    args = parser.parse_args()

    if not args.script.exists():
        print(f"Error: Script '{args.script}' not found.", file=sys.stderr)
        sys.exit(1)

    extra_files = args.file if args.file else None
    output_path = package_with_script(
        args.script,
        args.output,
        include_packages=args.packages,
        extra_files=extra_files
    )
    print(f"Created self-contained script: {output_path}")

if __name__ == '__main__':
    main()
