#!/usr/bin/env python3
"""Tagalong - Pack your Python script with its dependencies

# Installation

```bash
pip install tagalong
```

Or better, since this is a command-line tool:
```bash
pipx install tagalong
```

# Usage

```bash
tagalong script.py
```

This creates a self-contained executable that includes all non-stdlib dependencies.

Options:
- `-o, --output` - Specify output file
- `-r, --resources` - Include non-Python files
- `-p, --packages` - Include complete packages
- `-f, --file FILE ARCNAME` - Add extra files to the bundle

# How it works

Tagalong appends a ZIP archive with all dependencies to your script, making it
completely self-contained while still being a valid Python script.
Running the script automatically adds the ZIP to Python's import path.

The script portion can still be edited after packaging - just ensure your editor
preserves binary data and doesn't add a newline at EOF. For vim, use:
    vim -b script_packaged.py   # binary mode
or:
    vim +'set nofixeol' script_packaged.py   # don't add final newline

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
import os
import sys
import zipfile
import codecs
from importlib.util import find_spec
from io import BytesIO

# Get stdlib base path using a known stdlib module
STDLIB_PATH = os.path.dirname(os.path.dirname(codecs.__file__))

# Bootstrap code that will be saved as __main__.py in the zip
BOOTSTRAP_CODE = b"""exec(compile(
    # The zip from which __main__ was loaded:
    open(__loader__.archive, 'rb').read(
        # minimum offset = size of file before zip start:
        min(f[4] for f in __loader__._files.values())
    ).decode('utf8'),
    __loader__.archive,     # set filename in code object
    'exec'                  # compile in 'exec' mode
))"""

def get_dependencies(script_path):
    """Find all dependencies of a Python script."""
    finder = modulefinder.ModuleFinder()
    finder.run_script(script_path)
    return finder.modules.keys()

def is_stdlib_module(module_name):
    """Check if a module is part of the Python standard library."""
    if module_name == '__main__':
        return True

    try:
        spec = find_spec(module_name)
        if spec is None or not spec.has_location:  # Built-in or frozen modules
            return True

        return os.path.abspath(spec.origin).startswith(STDLIB_PATH)
    except (ImportError, AttributeError):
        return False

def get_script_content(script_path):
    """Read script content and truncate any existing ZIP."""
    try:
        # Try to open it as a zip file first
        with zipfile.ZipFile(script_path, 'r') as zf:
            # Get the minimum offset - same method used in __main__.py
            zip_start = min(f[4] for f in zf.filelist)
            # Read only the script portion
            with open(script_path, 'rb') as f:
                return f.read(zip_start)
    except zipfile.BadZipFile:
        # Not a zip file, return entire content
        with open(script_path, 'rb') as f:
            return f.read()

def find_base_dir(spec):
    """Find which sys.path entry a module is under."""
    module_path = os.path.abspath(spec.origin)
    for path in sys.path:
        path = os.path.abspath(path)
        if module_path.startswith(path + os.sep):
            return path
    return None

def add_file_to_zip(zipf, file_path, arcname, processed_files):
    """Add a file to the zip if not already processed."""
    if arcname in processed_files:
        return False
    processed_files.add(arcname)
    zipf.write(file_path, arcname)
    return True

def package_with_script(script_path, output_path=None, *, include_resources=False,
                       include_packages=False, extra_files=None):
    """
    Package dependencies with the script and create self-contained file.

    Args:
        script_path: Path to the Python script to package
        output_path: Output path for packaged script (default: script_packaged.py)
        include_resources: Include non-.py files from module directories
        include_packages: Include all files from packages, not just imported modules
        extra_files: List of (file_path, archive_path) tuples to add to the zip
    """
    if output_path is None:
        output_path = script_path.rsplit('.', 1)[0] + '_packaged.py'

    # Read original script, truncating any existing ZIP
    script_content = get_script_content(script_path)

    # Create zip in memory
    zip_buffer = BytesIO()
    processed_files = set()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Add __main__.py that executes the script portion
        zipf.writestr('__main__.py', BOOTSTRAP_CODE)

        # Add any extra files first
        if extra_files:
            for file_path, arc_path in extra_files:
                if os.path.exists(file_path):
                    add_file_to_zip(zipf, file_path, arc_path, processed_files)
                else:
                    print(f"Warning: Extra file not found: {file_path}", file=sys.stderr)

        # Package dependencies
        modules = get_dependencies(script_path)
        for module_name in modules:
            if is_stdlib_module(module_name):
                continue

            try:
                spec = find_spec(module_name)
                if not spec or not spec.has_location:
                    continue

                base_dir = find_base_dir(spec)
                if not base_dir:
                    print(f"Warning: Couldn't find base path for {module_name}", file=sys.stderr)
                    continue

                # Determine what directory to search for files
                if include_packages and spec.submodule_search_locations:
                    search_dir = spec.submodule_search_locations[0]
                else:
                    search_dir = os.path.dirname(spec.origin)
                    # If not including full package, just add this module
                    if not spec.submodule_search_locations:
                        arcname = os.path.relpath(spec.origin, base_dir)
                        add_file_to_zip(zipf, spec.origin, arcname, processed_files)
                        continue

                # Walk the directory for package files
                for root, _, files in os.walk(search_dir):
                    for file in files:
                        if file.endswith('.pyc'):
                            continue

                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, base_dir)

                        # Skip non-.py files unless requested
                        if not file.endswith('.py') and not include_resources:
                            continue

                        add_file_to_zip(zipf, file_path, arcname, processed_files)

            except Exception as e:
                print(f"Error packaging {module_name}: {e}", file=sys.stderr)

    # Create the final packaged script
    with open(output_path, 'wb') as f:
        f.write(script_content)
        f.write(b'\n\n')
        f.write(b'# === Bundled dependencies follow this line ===\n')
        f.write(zip_buffer.getvalue())

    # Make executable (rwxr-xr-x)
    os.chmod(output_path, 0o755)
    return output_path

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Package Python script with its dependencies')
    parser.add_argument('script', help='Python script to package')
    parser.add_argument('-o', '--output', help='Output path')
    parser.add_argument('-r', '--resources', action='store_true',
                       help='Include non-Python files from module directories')
    parser.add_argument('-p', '--packages', action='store_true',
                       help='Include complete packages, not just imported modules')
    parser.add_argument('-f', '--file', action='append', nargs=2,
                       metavar=('FILE', 'ARCNAME'),
                       help='Add extra file to zip with given archive path')

    args = parser.parse_args()

    if not os.path.exists(args.script):
        print(f"Error: Script '{args.script}' not found.", file=sys.stderr)
        sys.exit(1)

    extra_files = args.file if args.file else None
    output_path = package_with_script(
        args.script,
        args.output,
        include_resources=args.resources,
        include_packages=args.packages,
        extra_files=extra_files
    )
    print(f"Created self-contained script: {output_path}")

if __name__ == '__main__':
    main()
