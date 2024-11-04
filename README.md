CarryOn - pack your Python script with its dependencies

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
