#!/usr/bin/env python3
"""Carryon - Pack your Python script with its dependencies

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

def get_package_files(dist):
    """Get resolved paths of all files in a distribution (except .pyc)."""
    try:
        base = dist.locate_file('.').resolve()
        for file in dist.files:
            if not file.name.endswith('.pyc'):
                yield dist.locate_file(file).resolve(), base
    except Exception as e:
        print(f"Warning: Error processing package {dist.name}: {e}", 
              file=sys.stderr)

def build_package_map():
    """Build mapping of resolved file paths to their distribution packages."""
    import importlib.metadata  # Only imported when package mode is used
    package_map = {}  # Path -> (distribution, package_base)
    
    for dist in importlib.metadata.distributions():
        for path, base in get_package_files(dist):
            package_map[path] = (dist, base)
            
    return package_map

def add_file_to_zip(zipf, file_path, arcname, processed_files):
    """Add a file to the zip if not already processed."""
    if arcname in processed_files:
        return False
    processed_files.add(arcname)
    zipf.write(file_path, arcname)
    return True

def package_with_script(script_path, output_path=None, *, include_packages=False, 
                       extra_files=None):
    """
    Package dependencies with the script and create self-contained file.

    Args:
        script_path: Path to the Python script to package
        output_path: Output path for packaged script (default: script_carryon.py)
        include_packages: Include all files from packages
        extra_files: List of (file_path, archive_path) tuples to add to the zip
    """
    if output_path is None:
        output_path = script_path.parent / (script_path.stem + '_carryon' + script_path.suffix)

    script_content = get_script_content(script_path)
    
    # Create zip in memory
    zip_buffer = BytesIO()
    processed_files = set()
    processed_packages = set()
    package_map = {}
    
    if include_packages:
        try:
            package_map = build_package_map()
        except Exception as e:
            print(f"Error building package map: {e}", file=sys.stderr)
            sys.exit(1)
    
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

        # Package dependencies
        modules = get_dependencies(script_path)
        for module_name in modules:
            if module_name == '__main__' or is_stdlib_module(module_name):
                continue

            try:
                spec = find_spec(module_name)
            except (ModuleNotFoundError, ValueError) as e:
                print(f"Warning: Module not found {module_name}: {e}", file=sys.stderr)
                continue
            except Exception as e:
                print(f"Warning: Error finding module {module_name}: {e}", file=sys.stderr)
                continue

            if not spec or not spec.has_location:
                continue

            try:
                module_path = Path(spec.origin).resolve()
            except Exception as e:
                print(f"Warning: Error resolving path for {module_name}: {e}", file=sys.stderr)
                continue
                
            if include_packages and module_path in package_map:
                # Module belongs to a package
                try:
                    dist, package_base = package_map[module_path]
                    if dist.name not in processed_packages:
                        processed_packages.add(dist.name)
                        # Add all files from this package relative to package base
                        for path, _ in get_package_files(dist):
                            try:
                                arcname = path.relative_to(package_base)
                                add_file_to_zip(zipf, path, arcname, processed_files)
                            except Exception as e:
                                print(f"Warning: Error adding package file {path}: {e}", 
                                      file=sys.stderr)
                except Exception as e:
                    print(f"Warning: Error packaging files for {dist.name}: {e}", 
                          file=sys.stderr)
            else:
                # Non-packaged module
                try:
                    base_dir = find_base_dir(spec, script_path)
                    if base_dir:
                        arcname = module_path.relative_to(base_dir)
                        add_file_to_zip(zipf, module_path, arcname, processed_files)
                except Exception as e:
                    print(f"Warning: Error adding module {module_name}: {e}", file=sys.stderr)

    # Create the final packaged script
    output_path.write_bytes(script_content + 
                           b'\n\n# === Bundled dependencies follow this line ===\n' + 
                           zip_buffer.getvalue())

    # Make executable (rwxr-xr-x)
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