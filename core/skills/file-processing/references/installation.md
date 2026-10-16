# Dependency installation (file-processing)

## Installation command

Use the current Python interpreter:

```python
import subprocess
import sys

subprocess.check_call([
    sys.executable,
    "-m",
    "pip",
    "install",
    "PACKAGE_NAME"
])
```

This is preferred over:

```bash
pip install PACKAGE_NAME
```

because it guarantees that the package is installed into the Python environment being used by the tool.

## Install only what is needed

Do not blindly install a large collection of packages.

Install the dependency required by the current task.

For example:

```text
PDF → PyMuPDF
DOCX → python-docx
XLSX → openpyxl
PPTX → python-pptx
CSV → pandas
OCR → pytesseract + OCR engine
```

## Verify installation

After installation, immediately verify the import:

```python
try:
    import fitz
except ImportError as e:
    raise RuntimeError(
        "PyMuPDF installation failed or is unavailable."
    ) from e
```

Do not assume `pip` returning successfully means the package can necessarily be imported.

## Do Not Use Weak Substitutes Just to Avoid Dependencies

Avoid these patterns:

### Bad

```text
Need DOCX
↓
python-docx unavailable
↓
unzip DOCX manually
↓
parse XML manually
```

unless there is a specific reason that direct XML processing is required.

### Bad

```text
Need PDF
↓
PyMuPDF unavailable
↓
use an unrelated lightweight PDF library
```

### Bad

```text
Need XLSX
↓
openpyxl unavailable
↓
manually parse ZIP/XML
```

### Good

```text
Need DOCX
↓
python-docx missing
↓
install python-docx
↓
use python-docx
```

Manual parsing should be considered a specialized fallback, not the default solution.

## Environment and Package Discovery

Before installing, you may check whether a suitable package is already available:

```python
import importlib.util

if importlib.util.find_spec("fitz") is None:
    # install PyMuPDF
    ...
```

Remember that **import name and pip package name may differ**.

Examples:

| Function | pip package      | Python import |
| -------- | ---------------- | ------------- |
| PDF      | `PyMuPDF`        | `fitz`        |
| DOCX     | `python-docx`    | `docx`        |
| XLSX     | `openpyxl`       | `openpyxl`    |
| XLS      | `xlrd`           | `xlrd`        |
| XLSB     | `pyxlsb`         | `pyxlsb`      |
| PPTX     | `python-pptx`    | `pptx`        |
| HTML     | `beautifulsoup4` | `bs4`         |
| Image    | `Pillow`         | `PIL`         |

Never assume the pip package name is identical to the import name.

## Package Installation Failure

If installation fails:

1. Capture the actual installation error.
2. Determine whether:

   * network access is unavailable
   * the package requires a system dependency
   * the Python version is incompatible
   * the package name is incorrect
   * the environment does not permit installation
3. Try a compatible installation strategy.
4. Only then consider a fallback library.

Do not silently switch libraries and produce a lower-quality result.

If no viable solution exists, clearly report:

```text
Required dependency:
X

Installation result:
Y

Fallback attempted:
Z

Remaining limitation:
...
```
